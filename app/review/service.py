"""Cross-station review service: submission rules and the minute replay.

The service owns three decisions:

1. **Submission intake** – same station/day, a newer submission number replaces
   the whole delivery; an older number is rejected, an equal number with
   changed content is rejected, and an identical resend is a no-op.
2. **Minute replay** – every minute of a UTC day is evaluated in order against
   the latest complete deliveries. Two or more stations confirming the same
   canonical notice list issue/keep a review number; otherwise the minute is
   recorded as ``insufficient_evidence`` or ``disagreement``.
3. **Serving gate** – a review number only serves data while the most recently
   evaluated minute still supports its list; losing mutual support or changing
   the list invalidates it immediately.

Submission and the verdict writes it triggers share one SQLite transaction,
so a failure leaves the previous state in place. Per-day in-process locks
serialize writers; the database unique constraints keep the decision sound.
"""

from __future__ import annotations

import threading
from datetime import date, datetime, timezone
from typing import Protocol

from app.review.canonical import (
    canonical_notices,
    hash_notices,
    is_older_submission_no,
)
from app.review.models import (
    MinuteState,
    ReviewNotice,
    ReviewNumberNotServable,
    ReviewNumberRecord,
    SubmissionConflict,
    SubmissionRecord,
)
from app.review.store import ReviewStore

DAY_END_MINUTE = 24 * 60 - 1
MIN_SUPPORT_STATIONS = 2


class Clock(Protocol):
    def current_minute(self, day: date) -> int:
        """Return the minute-of-day to evaluate up to for ``day``."""


class SystemClock:
    """Real wall clock: the current UTC minute, clamped to the day's range."""

    def current_minute(self, day: date) -> int:
        now = datetime.now(timezone.utc)
        if now.date() != day:
            # A replay for a past day covers the whole day; a future day has no
            # evaluated minutes until the caller pins a minute explicitly.
            return DAY_END_MINUTE if now.date() > day else 0
        return min(now.hour * 60 + now.minute, DAY_END_MINUTE)


class FixedClock:
    """Test/demo clock pinned to one minute for every day.

    The pinned minute is the *open* minute. Intake evaluates closed minutes
    (strictly before it); the open minute itself is settled through an explicit
    ``evaluate(minute=...)`` call, mirroring how the real clock only finalises a
    minute once it has elapsed.
    """

    def __init__(self, minute: int) -> None:
        if not 0 <= minute <= DAY_END_MINUTE:
            raise ValueError("minute must be within 0..1439")
        self.minute = minute

    def current_minute(self, day: date) -> int:
        return self.minute


class ReviewService:
    def __init__(self, store: ReviewStore, clock: Clock | None = None) -> None:
        self.store = store
        self.clock = clock or SystemClock()
        self._locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()

    def _day_lock(self, day_text: str) -> threading.Lock:
        with self._locks_guard:
            lock = self._locks.get(day_text)
            if lock is None:
                lock = threading.Lock()
                self._locks[day_text] = lock
            return lock

    # -------------------------------------------------------------- intake

    def submit(
        self,
        day: date,
        station: str,
        submission_no: str,
        notices: list[ReviewNotice],
        complete: bool,
    ) -> SubmissionRecord:
        """Apply one station's whole-day delivery and replay the closed minutes.

        Only minutes that have elapsed are finalised, so a delivery arriving
        during the open minute cannot rewrite the immutable per-minute history;
        the open minute is settled later through :meth:`evaluate`.
        """
        day_text = day.isoformat()
        station = station.strip()
        submission_no = submission_no.strip()
        canonical = canonical_notices(notices)
        content_hash = hash_notices(canonical)

        lock = self._day_lock(day_text)
        with lock:
            existing = self.store.get_submission(day_text, station)
            if existing is not None:
                if is_older_submission_no(submission_no, existing.submission_no):
                    raise SubmissionConflict(
                        "stale_no",
                        f"submission_no {submission_no!r} is older than the "
                        f"accepted {existing.submission_no!r} for station "
                        f"{station!r} on {day_text}",
                    )
                if submission_no == existing.submission_no:
                    if existing.content_hash != content_hash:
                        raise SubmissionConflict(
                            "changed_payload",
                            f"submission_no {submission_no!r} was already accepted "
                            "with different content; a changed delivery needs a new "
                            "submission number",
                        )
                    # Identical resend: do not re-accept (no duplicate intake) and
                    # do not touch any existing verdicts.
                    return existing

            conn = self.store.transaction()
            try:
                record = self.store.replace_submission(
                    conn,
                    day_text,
                    station,
                    submission_no,
                    content_hash,
                    canonical,
                    complete,
                )
                closed = self._closed_minute(day)
                if closed is not None:
                    self._replay_locked(conn, day, target_minute=closed)
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()
            return record

    def _closed_minute(self, day: date) -> int | None:
        """Newest minute whose verdict may be finalised by an intake call.

        ``None`` means no minute has elapsed yet, so intake writes no verdict;
        the first minute is settled explicitly through :meth:`evaluate`.
        """
        if isinstance(self.clock, FixedClock):
            current = self.clock.minute
        else:
            current = self.clock.current_minute(day)
        return current - 1 if current > 0 else None

    # -------------------------------------------------------------- replay

    def evaluate(self, day: date, target_minute: int | None = None) -> list[MinuteState]:
        """Replay every not-yet-evaluated minute up to ``target_minute``.

        With ``target_minute=None`` the clock decides how far to go. Already
        recorded minutes are never rewritten, so historical verdicts stay
        intact; each call only fills the gap after the persisted cursor.
        """
        day_text = day.isoformat()
        lock = self._day_lock(day_text)
        with lock:
            conn = self.store.transaction()
            try:
                states = self._replay_locked(conn, day, target_minute)
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()
            return states

    def _replay_locked(
        self, conn, day: date, target_minute: int | None
    ) -> list[MinuteState]:
        day_text = day.isoformat()
        if target_minute is None:
            target_minute = self.clock.current_minute(day)
        if not 0 <= target_minute <= DAY_END_MINUTE:
            raise ValueError("minute must be within 0..1439")

        cursor = self.store.get_cursor(day_text, conn)
        start = 0 if cursor is None else cursor + 1
        written: list[MinuteState] = []
        if start > target_minute:
            # All minutes up to the target are already final: history is frozen.
            return written
        # The continuity anchor is the verdict of the minute immediately before
        # the first new minute, so support can carry a number forward.
        previous = (
            self.store.get_minute_state(day_text, start - 1, conn) if start > 0 else None
        )

        # Snapshot the latest deliveries once; every minute of this replay sees
        # the same submission set, exactly as a minute-by-minute scan would.
        submissions = self.store.latest_submissions(day_text, conn)
        for minute in range(start, target_minute + 1):
            state = self._evaluate_minute(
                conn, day_text, minute, submissions, previous
            )
            self.store.upsert_minute_state(
                conn,
                day_text,
                minute,
                status=state.status,
                review_no=state.review_no,
                content_hash=state.content_hash,
                stations=state.supporting_stations,
                detail=state.detail,
            )
            self.store.advance_cursor(conn, day_text, minute)
            written.append(state)
            previous = state
        return written

    def _evaluate_minute(
        self,
        conn,
        day_text: str,
        minute: int,
        submissions: list[SubmissionRecord],
        previous: MinuteState | None,
    ) -> MinuteState:
        complete = [s for s in submissions if s.complete]

        if len(complete) < MIN_SUPPORT_STATIONS:
            return MinuteState(
                day=day_text,
                minute=minute,
                status="insufficient_evidence",
                review_no=None,
                content_hash=None,
                supporting_stations=tuple(),
                detail=(
                    f"only {len(complete)} station(s) reported a complete "
                    f"delivery; at least {MIN_SUPPORT_STATIONS} are required"
                ),
                evaluated_at=_now(),
            )

        groups: dict[str, list[SubmissionRecord]] = {}
        for submission in complete:
            groups.setdefault(submission.content_hash, []).append(submission)

        winners = [group for group in groups.values() if len(group) >= MIN_SUPPORT_STATIONS]
        if not winners:
            detail = "stations reported different notice lists; no list is confirmed by two stations"
            return MinuteState(
                day=day_text,
                minute=minute,
                status="disagreement",
                review_no=None,
                content_hash=None,
                supporting_stations=tuple(),
                detail=detail,
                evaluated_at=_now(),
            )

        # A confirmed list wins. At most one hash can have two supporting
        # stations when only three stations exist; pick the largest group
        # deterministically if a configuration ever allows more.
        winners.sort(key=lambda group: (-len(group), group[0].content_hash))
        chosen = winners[0]
        chosen.sort(key=lambda s: s.station)
        content_hash = chosen[0].content_hash
        notices = chosen[0].notices
        stations = tuple(s.station for s in chosen)

        # Reuse the previous minute's number only while two-station support for
        # this exact list is continuous. Any break (insufficient evidence,
        # disagreement or another list) means a recovery must mint a new number.
        if (
            previous is not None
            and previous.status == "active"
            and previous.content_hash == content_hash
            and previous.review_no is not None
        ):
            review_no = previous.review_no
            detail = (
                f"list continuously confirmed by {len(stations)} station(s); "
                f"review number {review_no} retained"
            )
        else:
            record = self.store.issue_review_number(
                conn, day_text, content_hash, notices, minute
            )
            review_no = record.review_no
            detail = (
                f"list confirmed by {len(stations)} station(s); "
                f"review number {review_no} issued"
            )
        return MinuteState(
            day=day_text,
            minute=minute,
            status="active",
            review_no=review_no,
            content_hash=content_hash,
            supporting_stations=stations,
            detail=detail,
            evaluated_at=_now(),
        )

    # -------------------------------------------------------------- serving

    def serve(self, review_no: str) -> tuple[ReviewNumberRecord, MinuteState]:
        """Return the verified list only while the review number is active."""
        record = self.store.get_review_number_by_no(review_no.strip())
        if record is None:
            raise ReviewNumberNotServable(
                review_no, "unknown review number", None
            )

        cursor = self.store.get_cursor(record.day)
        if cursor is None:
            # No minute has ever been evaluated for that day: nothing can be
            # served yet, and the number only exists if one was, so treat this
            # as lost mutual support.
            raise ReviewNumberNotServable(
                review_no,
                "review number has no evaluated minute supporting it",
                None,
            )
        latest = self.store.get_minute_state(record.day, cursor)
        assert latest is not None
        if latest.status != "active" or latest.review_no != record.review_no:
            raise ReviewNumberNotServable(
                review_no,
                "review number no longer has mutual support for its list",
                latest.status,
            )
        return record, latest

    # -------------------------------------------------------------- views

    def timeline(self, day: date) -> list[MinuteState]:
        return self.store.minute_states(day.isoformat())

    def minute_state(self, day: date, minute: int) -> MinuteState | None:
        return self.store.get_minute_state(day.isoformat(), minute)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
