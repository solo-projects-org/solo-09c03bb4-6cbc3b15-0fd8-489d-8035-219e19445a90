"""Review service state-machine tests.

Covers the full cross-station lifecycle: submission-number rules, minute-by-
minute consensus, empty lists, order-insensitive comparison, immediate
invalidation when mutual support is lost, re-issue of a new number, number
reuse when only the supporting stations change, and restart persistence.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from app.review.models import (
    ReviewNotice,
    ReviewNumberNotServable,
    SubmissionConflict,
)
from app.review.service import FixedClock, ReviewService
from app.review.store import ReviewStore

DAY = date(2026, 11, 19)


def notice(no: str, title: str = "标题", at: str = "2026-11-19T08:00:00Z") -> ReviewNotice:
    return ReviewNotice(no, title, at)


LIST_A = [notice("HB-01", "甲公告"), notice("HB-02", "乙公告")]
LIST_A_REORDERED = [notice("HB-02", "乙公告"), notice("HB-01", "甲公告")]
LIST_B = [notice("HB-01", "甲公告（修订）"), notice("HB-02", "乙公告")]


@pytest.fixture()
def service(tmp_path: Path) -> ReviewService:
    store = ReviewStore(tmp_path / "review.sqlite3")
    store.init()
    return ReviewService(store, clock=FixedClock(0))


def submit(svc: ReviewService, station: str, no: str, notices, complete: bool = True):
    return svc.submit(DAY, station, no, list(notices), complete)


def settle(svc: ReviewService, minute: int = 0):
    """Finalise the (open) minute's verdict with the evidence held so far."""
    return svc.evaluate(DAY, minute)


# ------------------------------------------------------------ intake rules


def test_new_submission_replaces_whole_delivery(service: ReviewService) -> None:
    submit(service, "a", "S-1", LIST_A)
    record = submit(service, "a", "S-2", LIST_B)
    assert record.submission_no == "S-2"
    assert tuple(n.title for n in record.notices if n.announcement_no == "HB-01") == (
        "甲公告（修订）",
    )
    # Only the latest delivery is retained.
    assert len(service.store.latest_submissions(DAY.isoformat())) == 1


def test_older_submission_no_is_rejected_and_state_kept(service: ReviewService) -> None:
    from app.review.canonical import canonical_notices, hash_notices

    submit(service, "a", "S-2", LIST_A)
    with pytest.raises(SubmissionConflict) as exc:
        submit(service, "a", "S-1", LIST_B)
    assert exc.value.kind == "stale_no"
    record = service.store.get_submission(DAY.isoformat(), "a")
    assert record is not None and record.submission_no == "S-2"
    assert record.content_hash == hash_notices(canonical_notices(LIST_A))


def test_same_no_with_changed_payload_is_rejected(service: ReviewService) -> None:
    submit(service, "a", "S-1", LIST_A)
    with pytest.raises(SubmissionConflict) as exc:
        submit(service, "a", "S-1", LIST_B)
    assert exc.value.kind == "changed_payload"
    record = service.store.get_submission(DAY.isoformat(), "a")
    assert record is not None
    from app.review.canonical import hash_notices, canonical_notices

    assert record.content_hash == hash_notices(canonical_notices(LIST_A))


def test_identical_resend_is_not_re_accepted(service: ReviewService) -> None:
    first = submit(service, "a", "S-1", LIST_A)
    second = submit(service, "a", "S-1", LIST_A_REORDERED)  # same content, other order
    assert second.created_at == first.created_at
    assert second.content_hash == first.content_hash


def test_numeric_submission_no_orders_naturally(service: ReviewService) -> None:
    submit(service, "a", "S-9", LIST_A)
    with pytest.raises(SubmissionConflict):
        submit(service, "a", "S-8", LIST_A)
    # S-10 must be accepted as newer than S-9.
    record = submit(service, "a", "S-10", LIST_A)
    assert record.submission_no == "S-10"


# ------------------------------------------------------------ minute consensus


def test_single_station_is_insufficient_evidence(service: ReviewService) -> None:
    submit(service, "a", "S-1", LIST_A)
    settle(service, 0)
    state = service.minute_state(DAY, 0)
    assert state is not None
    assert state.status == "insufficient_evidence"
    assert state.review_no is None


def test_two_stations_same_list_issue_a_review_number(service: ReviewService) -> None:
    submit(service, "a", "S-1", LIST_A)
    submit(service, "b", "T-1", LIST_A_REORDERED)  # different row order
    settle(service, 0)
    state = service.minute_state(DAY, 0)
    assert state.status == "active"
    assert state.review_no is not None
    assert set(state.supporting_stations) == {"a", "b"}

    record, latest = service.serve(state.review_no)
    assert [n.announcement_no for n in record.notices] == ["HB-01", "HB-02"]
    assert latest.review_no == record.review_no


def test_empty_list_confirmed_by_two_stations_also_issues_number(
    service: ReviewService,
) -> None:
    submit(service, "a", "S-1", [])
    submit(service, "b", "T-1", [])
    settle(service, 0)
    state = service.minute_state(DAY, 0)
    assert state.status == "active"
    record, _ = service.serve(state.review_no)
    assert record.notices == ()


def test_disagreement_is_marked_separately_from_insufficient_evidence(
    service: ReviewService,
) -> None:
    submit(service, "a", "S-1", LIST_A)
    submit(service, "b", "T-1", LIST_B)
    settle(service, 0)
    state = service.minute_state(DAY, 0)
    assert state.status == "disagreement"
    assert state.review_no is None


def test_incomplete_delivery_does_not_count_as_support(service: ReviewService) -> None:
    submit(service, "a", "S-1", LIST_A, complete=True)
    submit(service, "b", "T-1", LIST_A, complete=False)  # still collecting
    settle(service, 0)
    state = service.minute_state(DAY, 0)
    assert state.status == "insufficient_evidence"


def test_two_of_three_confirming_is_enough(service: ReviewService) -> None:
    submit(service, "a", "S-1", LIST_A)
    submit(service, "b", "T-1", LIST_A)
    submit(service, "c", "U-1", LIST_B)
    settle(service, 0)
    state = service.minute_state(DAY, 0)
    assert state.status == "active"
    assert set(state.supporting_stations) == {"a", "b"}


# ------------------------------------------------------------ invalidation / reissue


def test_losing_support_invalidates_the_old_number_immediately(
    service: ReviewService,
) -> None:
    submit(service, "a", "S-1", LIST_A)
    submit(service, "b", "T-1", LIST_A)
    settle(service, 0)
    number = service.minute_state(DAY, 0).review_no
    assert service.serve(number)[0].review_no == number

    # Station b replaces its delivery with a different list: support collapses.
    submit(service, "b", "T-2", LIST_B)
    service.evaluate(DAY, 1)
    minute1 = service.minute_state(DAY, 1)
    assert minute1.status == "disagreement"

    with pytest.raises(ReviewNumberNotServable) as exc:
        service.serve(number)
    assert exc.value.status == "disagreement"
    # The historical minute still records the number that was valid then.
    assert service.minute_state(DAY, 0).review_no == number


def test_recovered_support_issues_a_new_number(service: ReviewService) -> None:
    submit(service, "a", "S-1", LIST_A)
    submit(service, "b", "T-1", LIST_A)
    settle(service, 0)
    first = service.minute_state(DAY, 0).review_no

    submit(service, "b", "T-2", LIST_B)  # break support
    service.evaluate(DAY, 1)
    with pytest.raises(ReviewNumberNotServable):
        service.serve(first)

    submit(service, "b", "T-3", LIST_A)  # same list recovers
    service.evaluate(DAY, 2)
    minute2 = service.minute_state(DAY, 2)
    assert minute2.status == "active"
    # A break means a fresh number even though the list is identical.
    assert minute2.review_no != first
    record, _ = service.serve(minute2.review_no)
    assert [n.announcement_no for n in record.notices] == ["HB-01", "HB-02"]


def test_changed_list_issues_a_new_number_and_kills_the_old(
    service: ReviewService,
) -> None:
    submit(service, "a", "S-1", LIST_A)
    submit(service, "b", "T-1", LIST_A)
    settle(service, 0)
    old = service.minute_state(DAY, 0).review_no

    submit(service, "a", "S-2", LIST_B)
    submit(service, "b", "T-2", LIST_B)
    service.evaluate(DAY, 1)
    new = service.minute_state(DAY, 1).review_no
    assert new != old
    with pytest.raises(ReviewNumberNotServable):
        service.serve(old)
    record, latest = service.serve(new)
    assert latest.review_no == new
    assert tuple(n.title for n in record.notices if n.announcement_no == "HB-01") == (
        "甲公告（修订）",
    )


def test_supporting_station_swap_keeps_the_original_number(
    service: ReviewService,
) -> None:
    submit(service, "a", "S-1", LIST_A)
    submit(service, "b", "T-1", LIST_A)
    settle(service, 0)
    original = service.minute_state(DAY, 0).review_no

    # Continuous two-station support, but the supporters change from a+b to a+c.
    submit(service, "c", "U-1", LIST_A)
    submit(service, "b", "T-2", LIST_B)  # b leaves the consensus simultaneously
    service.evaluate(DAY, 1)
    minute1 = service.minute_state(DAY, 1)
    assert minute1.status == "active"
    assert minute1.review_no == original
    assert set(minute1.supporting_stations) == {"a", "c"}
    # Same number still serves.
    assert service.serve(original)[0].review_no == original


def test_review_numbers_are_unique_per_day_and_sequential(service: ReviewService) -> None:
    submit(service, "a", "S-1", LIST_A)
    submit(service, "b", "T-1", LIST_A)
    settle(service, 0)
    first = service.minute_state(DAY, 0).review_no
    assert first == f"RV-{DAY.strftime('%Y%m%d')}-001"

    submit(service, "a", "S-2", LIST_B)
    submit(service, "b", "T-2", LIST_B)
    service.evaluate(DAY, 1)
    second = service.minute_state(DAY, 1).review_no
    assert second == f"RV-{DAY.strftime('%Y%m%d')}-002"


def test_unknown_review_number_is_not_servable(service: ReviewService) -> None:
    with pytest.raises(ReviewNumberNotServable):
        service.serve("RV-19000101-001")


# ------------------------------------------------------------ failure atomicity & restart


def test_failed_submission_keeps_the_previous_state(service: ReviewService) -> None:
    submit(service, "a", "S-1", LIST_A)
    before = service.store.get_submission(DAY.isoformat(), "a")

    # A duplicate announcement inside a delivery aborts the whole transaction.
    with pytest.raises(Exception):
        service.submit(
            DAY,
            "a",
            "S-2",
            [notice("X"), notice("X")],
            True,
        )
    after = service.store.get_submission(DAY.isoformat(), "a")
    assert after == before
    # No minute verdict exists yet: intake only finalises closed minutes.
    assert service.timeline(DAY) == []


def test_restart_resumes_the_replay_and_keeps_numbers(tmp_path: Path) -> None:
    db_path = tmp_path / "review.sqlite3"
    store = ReviewStore(db_path)
    store.init()
    first = ReviewService(store, clock=FixedClock(0))
    first.submit(DAY, "a", "S-1", LIST_A, True)
    first.submit(DAY, "b", "T-1", LIST_A, True)
    first.evaluate(DAY, 0)  # settle the open minute before the "restart"
    number = first.minute_state(DAY, 0).review_no

    # Restart: brand-new service objects over the same SQLite file.
    store2 = ReviewStore(db_path)
    store2.init()
    second = ReviewService(store2, clock=FixedClock(5))
    states = second.evaluate(DAY, 5)
    assert [s.minute for s in states] == [1, 2, 3, 4, 5]
    # The original number stays valid and serves after restart.
    record, latest = second.serve(number)
    assert record.review_no == number
    assert latest.minute == 5
    assert second.minute_state(DAY, 0).review_no == number


def test_timeline_reports_every_minute_verdict(service: ReviewService) -> None:
    # Only station A for minutes 0..1: evidence stays insufficient.
    submit(service, "a", "S-1", LIST_A)
    service.evaluate(DAY, 1)
    # Station B joins from minute 2 onward; past minutes keep their snapshot.
    submit(service, "b", "T-1", LIST_A)
    service.evaluate(DAY, 3)
    timeline = service.timeline(DAY)
    assert [s.minute for s in timeline] == [0, 1, 2, 3]
    assert {s.status for s in timeline[:2]} == {"insufficient_evidence"}
    assert {s.status for s in timeline[2:]} == {"active"}
    # Immutable history: minute 0 was never retroactively upgraded.
    assert timeline[0].review_no is None
    assert timeline[2].review_no is not None
