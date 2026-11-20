"""End-to-end API tests for the review submission and serving endpoints."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.db import Database
from app.main import AppState, set_state_for_tests
from app.main import app as archive_app
from app.review.service import FixedClock, ReviewService
from app.review.store import ReviewStore

DAY = "2026-11-19"


def notice(no: str, title: str = "甲公告", at: str = f"{DAY}T08:00:00Z") -> dict:
    return {"announcement_no": no, "title": title, "published_at": at}


LIST_A = [notice("HB-01"), notice("HB-02", "乙公告", f"{DAY}T12:00:00Z")]
LIST_B = [notice("HB-01", "甲公告（修订）"), notice("HB-02", "乙公告", f"{DAY}T12:00:00Z")]


@pytest.fixture()
def client(tmp_path: Path) -> TestClient:
    db = Database(tmp_path / "api.sqlite3")
    db.init()
    review_store = ReviewStore(tmp_path / "api.sqlite3")
    review_store.init()
    review = ReviewService(review_store, clock=FixedClock(0))
    state = AppState(
        db=db,
        client=None,  # review endpoints never call the source
        collector=None,
        review=review,
    )
    set_state_for_tests(state)
    with TestClient(archive_app) as test_client:
        yield test_client


def submit(client: TestClient, station: str, no: str, notices, complete: bool = True):
    return client.post(
        f"/api/review/days/{DAY}/submissions",
        json={
            "station": station,
            "submission_no": no,
            "complete": complete,
            "notices": notices,
        },
    )


def evaluate(client: TestClient, minute: int):
    return client.post(
        f"/api/review/days/{DAY}/evaluation", json={"minute": minute}
    )


def test_swagger_documents_the_review_api(client: TestClient) -> None:
    assert client.get("/docs").status_code == 200
    paths = client.get("/openapi.json").json()["paths"]
    assert "/api/review/days/{day}/submissions" in paths
    assert "/api/review/days/{day}/evaluation" in paths
    assert "/api/review/days/{day}/minutes/{minute}" in paths
    assert "/api/review/numbers/{review_no}" in paths


def test_submission_then_consensus_issue_and_serve(client: TestClient) -> None:
    first = submit(client, "station-a", "S-001", LIST_A)
    assert first.status_code == 200, first.text
    assert first.json()["accepted"] is True
    assert first.json()["notice_count"] == 2

    # No minute is final until it is evaluated; minute 0 first sees one station.
    evaluate(client, 0)
    minute0 = client.get(f"/api/review/days/{DAY}/minutes/0").json()
    assert minute0["status"] == "insufficient_evidence"
    assert minute0["review_no"] is None
    assert minute0["minute_label"] == "00:00"

    # Row order differs but the canonical list is the same. The new consensus
    # is recorded on the next minute because minute 0 is already frozen.
    second = submit(client, "station-b", "T-001", list(reversed(LIST_A)))
    assert second.status_code == 200
    evaluate(client, 1)
    active = client.get(f"/api/review/days/{DAY}/minutes/1").json()
    assert active["status"] == "active"
    review_no = active["review_no"]
    assert review_no and review_no.startswith("RV-20261119-")
    assert set(active["supporting_stations"]) == {"station-a", "station-b"}

    served = client.get(f"/api/review/numbers/{review_no}")
    assert served.status_code == 200
    body = served.json()
    assert body["day"] == DAY
    assert {n["announcement_no"] for n in body["notices"]} == {"HB-01", "HB-02"}
    assert body["latest_minute"] == 1


def test_identical_resend_is_idempotent(client: TestClient) -> None:
    first = submit(client, "station-a", "S-001", LIST_A).json()
    second = submit(client, "station-a", "S-001", LIST_A).json()
    assert second["accepted"] is False
    assert second["created_at"] == first["created_at"]
    assert second["content_hash"] == first["content_hash"]


def test_old_number_and_same_number_change_are_conflicts(client: TestClient) -> None:
    submit(client, "station-a", "S-002", LIST_A)

    stale = submit(client, "station-a", "S-001", LIST_B)
    assert stale.status_code == 409
    assert stale.json()["detail"]["kind"] == "stale_no"

    changed = submit(client, "station-a", "S-002", LIST_B)
    assert changed.status_code == 409
    assert changed.json()["detail"]["kind"] == "changed_payload"


def test_empty_list_consensus_serves_an_empty_payload(client: TestClient) -> None:
    submit(client, "station-a", "S-001", [])
    submit(client, "station-b", "T-001", [])
    evaluate(client, 0)
    review_no = client.get(f"/api/review/days/{DAY}/minutes/0").json()["review_no"]
    body = client.get(f"/api/review/numbers/{review_no}").json()
    assert body["notices"] == []


def test_disagreement_and_evidence_are_distinguished(client: TestClient) -> None:
    submit(client, "station-a", "S-001", LIST_A)
    disagreement = submit(client, "station-b", "T-001", LIST_B)
    assert disagreement.status_code == 200
    evaluate(client, 0)
    body = client.get(f"/api/review/days/{DAY}/minutes/0").json()
    assert body["status"] == "disagreement"
    assert body["detail"]

    # A third station agreeing with a makes the two-station threshold, recorded
    # on the next minute because minute 0 is immutable.
    submit(client, "station-c", "U-001", LIST_A)
    evaluated = evaluate(client, 1).json()
    assert evaluated["status"] == "active"
    assert set(evaluated["supporting_stations"]) == {"station-a", "station-c"}


def test_invalidation_and_reissue_lifecycle(client: TestClient) -> None:
    submit(client, "station-a", "S-001", LIST_A)
    submit(client, "station-b", "T-001", LIST_A)
    evaluate(client, 0)
    old_no = client.get(f"/api/review/days/{DAY}/minutes/0").json()["review_no"]
    assert client.get(f"/api/review/numbers/{old_no}").status_code == 200

    # Break mutual support: b changes its list at the next minute.
    submit(client, "station-b", "T-002", LIST_B)
    evaluate(client, 1)
    blocked = client.get(f"/api/review/numbers/{old_no}")
    assert blocked.status_code == 409
    assert blocked.json()["detail"]["latest_status"] == "disagreement"

    # Recover with the same list: a brand-new number is issued.
    submit(client, "station-b", "T-003", LIST_A)
    evaluate(client, 2)
    minute2 = client.get(f"/api/review/days/{DAY}/minutes/2").json()
    assert minute2["status"] == "active"
    new_no = minute2["review_no"]
    assert new_no != old_no
    assert client.get(f"/api/review/numbers/{new_no}").status_code == 200


def test_station_swap_keeps_the_same_review_number(client: TestClient) -> None:
    submit(client, "station-a", "S-001", LIST_A)
    submit(client, "station-b", "T-001", LIST_A)
    evaluate(client, 0)
    original = client.get(f"/api/review/days/{DAY}/minutes/0").json()["review_no"]

    submit(client, "station-c", "U-001", LIST_A)
    submit(client, "station-b", "T-002", LIST_B)
    client.post(f"/api/review/days/{DAY}/evaluation", json={"minute": 1})
    minute1 = client.get(f"/api/review/days/{DAY}/minutes/1").json()
    assert minute1["review_no"] == original
    assert set(minute1["supporting_stations"]) == {"station-a", "station-c"}
    assert client.get(f"/api/review/numbers/{original}").status_code == 200


def test_timeline_and_unknown_minute_handling(client: TestClient) -> None:
    submit(client, "station-a", "S-001", LIST_A)
    # Intake alone writes no verdicts; minutes are finalised by evaluation.
    assert client.get(f"/api/review/days/{DAY}/minutes").json() == []
    evaluate(client, 0)
    timeline = client.get(f"/api/review/days/{DAY}/minutes").json()
    assert [row["minute"] for row in timeline] == [0]

    missing = client.get(f"/api/review/days/{DAY}/minutes/42")
    assert missing.status_code == 404

    out_of_range = client.get(f"/api/review/days/{DAY}/minutes/1440")
    assert out_of_range.status_code == 422


def test_invalid_day_and_bad_payloads_are_422(client: TestClient) -> None:
    assert client.post(
        "/api/review/days/2026-02-30/submissions",
        json={"station": "a", "submission_no": "1", "notices": []},
    ).status_code == 422

    bad_time = client.post(
        f"/api/review/days/{DAY}/submissions",
        json={
            "station": "a",
            "submission_no": "1",
            "notices": [notice("HB-X", at="2026-11-20T08:00:00Z")],
        },
    )
    assert bad_time.status_code == 422

    duplicate = client.post(
        f"/api/review/days/{DAY}/submissions",
        json={
            "station": "a",
            "submission_no": "1",
            "notices": [notice("HB-X"), notice("HB-X")],
        },
    )
    assert duplicate.status_code == 422

    blank_station = client.post(
        f"/api/review/days/{DAY}/submissions",
        json={"station": "  ", "submission_no": "1", "notices": []},
    )
    assert blank_station.status_code == 422


def test_unknown_review_number_returns_409(client: TestClient) -> None:
    response = client.get("/api/review/numbers/RV-19000101-001")
    assert response.status_code == 409
