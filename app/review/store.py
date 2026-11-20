"""SQLite persistence for cross-station review state.

Four tables hold the whole review flow durably:

* ``review_submissions``  – the latest whole-day delivery of every station,
  keyed by (day, station); submission numbers and content hashes guard the
  reject-old-number / reject-same-number-changed-payload rules.
* ``review_minute_states`` – the immutable per-minute verdict produced by the
  minute-by-minute replay, including any review number and support stations.
* ``review_numbers``      – every review number ever issued. The same notice
  list can receive several numbers over a day: a number is only reused while
  two-station support for that list is continuous, so recovery after a break
  always mints a new number.
* ``review_day_cursors``  – the last evaluated minute of each day, so a restart
  resumes the replay instead of starting the day over.

A submission and all verdict writes it triggers happen inside one transaction:
on failure the previous state is left untouched. Every helper accepts an open
connection so the replay's reads and writes share one transaction — opening a
second connection while the write transaction is open would self-deadlock.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from app.review.models import (
    MinuteState,
    ReviewNotice,
    ReviewNumberRecord,
    SubmissionRecord,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS review_submissions (
    day TEXT NOT NULL,
    station TEXT NOT NULL,
    submission_no TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    notices_json TEXT NOT NULL,
    complete INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (day, station)
);

CREATE TABLE IF NOT EXISTS review_minute_states (
    day TEXT NOT NULL,
    minute INTEGER NOT NULL,
    status TEXT NOT NULL,
    review_no TEXT,
    content_hash TEXT,
    stations_json TEXT NOT NULL,
    detail TEXT,
    evaluated_at TEXT NOT NULL,
    PRIMARY KEY (day, minute)
);

CREATE INDEX IF NOT EXISTS idx_review_minutes_day
    ON review_minute_states(day, minute);

CREATE TABLE IF NOT EXISTS review_numbers (
    review_no TEXT PRIMARY KEY,
    day TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    notices_json TEXT NOT NULL,
    first_minute INTEGER NOT NULL,
    issued_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_review_numbers_day
    ON review_numbers(day);

CREATE INDEX IF NOT EXISTS idx_review_numbers_day_hash
    ON review_numbers(day, content_hash);

CREATE TABLE IF NOT EXISTS review_day_cursors (
    day TEXT PRIMARY KEY,
    last_minute INTEGER NOT NULL,
    updated_at TEXT NOT NULL
);
"""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _decode_notices(payload: str) -> tuple[ReviewNotice, ...]:
    raw = json.loads(payload)
    return tuple(
        ReviewNotice(
            announcement_no=entry["announcement_no"],
            title=entry["title"],
            published_at=entry["published_at"],
        )
        for entry in raw
    )


class ReviewStore:
    """SQLite persistence; one connection per operation unless one is supplied."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def init(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = self._connect()
        try:
            conn.executescript(SCHEMA)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.commit()
        finally:
            conn.close()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def transaction(self) -> sqlite3.Connection:
        """Open a write transaction; the caller commits and closes it."""
        conn = self._connect()
        conn.execute("BEGIN IMMEDIATE")
        return conn

    # -------------------------------------------------------------- submissions

    def get_submission(
        self, day: str, station: str, conn: sqlite3.Connection | None = None
    ) -> SubmissionRecord | None:
        own = conn is None
        c = conn or self._connect()
        try:
            row = c.execute(
                "SELECT day, station, submission_no, content_hash, notices_json, "
                "complete, created_at FROM review_submissions "
                "WHERE day = ? AND station = ?",
                (day, station),
            ).fetchone()
        finally:
            if own:
                c.close()
        if row is None:
            return None
        return self._submission_from_row(row)

    def latest_submissions(
        self, day: str, conn: sqlite3.Connection | None = None
    ) -> list[SubmissionRecord]:
        own = conn is None
        c = conn or self._connect()
        try:
            rows = c.execute(
                "SELECT day, station, submission_no, content_hash, notices_json, "
                "complete, created_at FROM review_submissions WHERE day = ? "
                "ORDER BY station",
                (day,),
            ).fetchall()
        finally:
            if own:
                c.close()
        return [self._submission_from_row(row) for row in rows]

    @staticmethod
    def _submission_from_row(row: sqlite3.Row) -> SubmissionRecord:
        return SubmissionRecord(
            day=row["day"],
            station=row["station"],
            submission_no=row["submission_no"],
            content_hash=row["content_hash"],
            notices=_decode_notices(row["notices_json"]),
            complete=bool(row["complete"]),
            created_at=row["created_at"],
        )

    def replace_submission(
        self,
        conn: sqlite3.Connection,
        day: str,
        station: str,
        submission_no: str,
        content_hash: str,
        notices: tuple[ReviewNotice, ...],
        complete: bool,
    ) -> SubmissionRecord:
        """Insert or wholesale-replace a station's delivery inside ``conn``'s txn."""
        conn.execute(
            "INSERT INTO review_submissions("
            "day, station, submission_no, content_hash, notices_json, "
            "complete, created_at) VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(day, station) DO UPDATE SET "
            "submission_no=excluded.submission_no, "
            "content_hash=excluded.content_hash, "
            "notices_json=excluded.notices_json, "
            "complete=excluded.complete, created_at=excluded.created_at",
            (
                day,
                station,
                submission_no,
                content_hash,
                json.dumps([n.as_dict() for n in notices], ensure_ascii=False),
                1 if complete else 0,
                _utc_now(),
            ),
        )
        row = conn.execute(
            "SELECT day, station, submission_no, content_hash, notices_json, "
            "complete, created_at FROM review_submissions "
            "WHERE day = ? AND station = ?",
            (day, station),
        ).fetchone()
        assert row is not None
        return self._submission_from_row(row)

    # ------------------------------------------------------------- minute states

    def upsert_minute_state(
        self,
        conn: sqlite3.Connection,
        day: str,
        minute: int,
        status: str,
        review_no: str | None,
        content_hash: str | None,
        stations: tuple[str, ...],
        detail: str | None,
    ) -> None:
        conn.execute(
            "INSERT INTO review_minute_states("
            "day, minute, status, review_no, content_hash, stations_json, "
            "detail, evaluated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(day, minute) DO UPDATE SET "
            "status=excluded.status, review_no=excluded.review_no, "
            "content_hash=excluded.content_hash, stations_json=excluded.stations_json, "
            "detail=excluded.detail, evaluated_at=excluded.evaluated_at",
            (
                day,
                minute,
                status,
                review_no,
                content_hash,
                json.dumps(list(stations)),
                detail,
                _utc_now(),
            ),
        )

    def get_minute_state(
        self, day: str, minute: int, conn: sqlite3.Connection | None = None
    ) -> MinuteState | None:
        own = conn is None
        c = conn or self._connect()
        try:
            row = c.execute(
                "SELECT day, minute, status, review_no, content_hash, "
                "stations_json, detail, evaluated_at FROM review_minute_states "
                "WHERE day = ? AND minute = ?",
                (day, minute),
            ).fetchone()
        finally:
            if own:
                c.close()
        return None if row is None else self._minute_from_row(row)

    def minute_states(self, day: str) -> list[MinuteState]:
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT day, minute, status, review_no, content_hash, "
                "stations_json, detail, evaluated_at FROM review_minute_states "
                "WHERE day = ? ORDER BY minute",
                (day,),
            ).fetchall()
        finally:
            conn.close()
        return [self._minute_from_row(row) for row in rows]

    @staticmethod
    def _minute_from_row(row: sqlite3.Row) -> MinuteState:
        return MinuteState(
            day=row["day"],
            minute=row["minute"],
            status=row["status"],
            review_no=row["review_no"],
            content_hash=row["content_hash"],
            supporting_stations=tuple(json.loads(row["stations_json"])),
            detail=row["detail"],
            evaluated_at=row["evaluated_at"],
        )

    # ------------------------------------------------------------- review numbers

    def get_review_number_by_no(
        self, review_no: str, conn: sqlite3.Connection | None = None
    ) -> ReviewNumberRecord | None:
        own = conn is None
        c = conn or self._connect()
        try:
            row = c.execute(
                "SELECT review_no, day, content_hash, notices_json, first_minute, "
                "issued_at FROM review_numbers WHERE review_no = ?",
                (review_no,),
            ).fetchone()
        finally:
            if own:
                c.close()
        return None if row is None else self._number_from_row(row)

    def issue_review_number(
        self,
        conn: sqlite3.Connection,
        day: str,
        content_hash: str,
        notices: tuple[ReviewNotice, ...],
        minute: int,
    ) -> ReviewNumberRecord:
        """Mint the next sequential review number for a day.

        Reuse decisions live in the service: this method only allocates a fresh
        number, so a list that recovers after losing support gets a new one.
        """
        seq = (
            conn.execute(
                "SELECT COUNT(*) AS n FROM review_numbers WHERE day = ?", (day,)
            ).fetchone()["n"]
            + 1
        )
        review_no = f"RV-{day.replace('-', '')}-{seq:03d}"
        conn.execute(
            "INSERT INTO review_numbers("
            "review_no, day, content_hash, notices_json, first_minute, issued_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                review_no,
                day,
                content_hash,
                json.dumps([n.as_dict() for n in notices], ensure_ascii=False),
                minute,
                _utc_now(),
            ),
        )
        row = conn.execute(
            "SELECT review_no, day, content_hash, notices_json, first_minute, "
            "issued_at FROM review_numbers WHERE review_no = ?",
            (review_no,),
        ).fetchone()
        assert row is not None
        return self._number_from_row(row)

    @staticmethod
    def _number_from_row(row: sqlite3.Row) -> ReviewNumberRecord:
        return ReviewNumberRecord(
            review_no=row["review_no"],
            day=row["day"],
            content_hash=row["content_hash"],
            notices=_decode_notices(row["notices_json"]),
            first_minute=row["first_minute"],
            issued_at=row["issued_at"],
        )

    # ------------------------------------------------------------------ cursors

    def get_cursor(
        self, day: str, conn: sqlite3.Connection | None = None
    ) -> int | None:
        own = conn is None
        c = conn or self._connect()
        try:
            row = c.execute(
                "SELECT last_minute FROM review_day_cursors WHERE day = ?", (day,)
            ).fetchone()
        finally:
            if own:
                c.close()
        return None if row is None else row["last_minute"]

    def advance_cursor(
        self, conn: sqlite3.Connection, day: str, minute: int
    ) -> None:
        conn.execute(
            "INSERT INTO review_day_cursors(day, last_minute, updated_at) "
            "VALUES (?, ?, ?) ON CONFLICT(day) DO UPDATE SET "
            "last_minute=excluded.last_minute, updated_at=excluded.updated_at",
            (day, minute, _utc_now()),
        )
