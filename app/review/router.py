"""FastAPI router for the review (复核) submission and serving API."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from app.review.canonical import DuplicateAnnouncement
from app.review.models import (
    ReviewNotice,
    ReviewNumberNotServable,
    SubmissionConflict,
)
from app.review.schemas import (
    EvaluateIn,
    MinuteStateOut,
    ReviewServeOut,
    ReviewSubmissionIn,
    SubmissionOut,
)
from app.review.service import ReviewService
from common.times import parse_archive_date, parse_published_at

router = APIRouter(prefix="/api/review", tags=["review"])


def service() -> ReviewService:
    # Local import keeps the composition root free of import cycles.
    from app.main import archive

    return archive().review


def _parse_day(day: str):
    try:
        return parse_archive_date(day)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def _build_notices(payload: ReviewSubmissionIn, day) -> list[ReviewNotice]:
    notices: list[ReviewNotice] = []
    for entry in payload.notices:
        try:
            normalised, _minute = parse_published_at(entry.published_at, day)
        except ValueError as exc:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"notice {entry.announcement_no}: {exc}"
                    if entry.announcement_no
                    else str(exc)
                ),
            ) from exc
        if not entry.announcement_no.strip() or not entry.title.strip():
            raise HTTPException(
                status_code=422,
                detail="every notice needs a non-empty announcement_no and title",
            )
        notices.append(
            ReviewNotice(
                announcement_no=entry.announcement_no,
                title=entry.title,
                published_at=normalised,
            )
        )
    return notices


@router.post("/days/{day}/submissions", response_model=SubmissionOut)
def submit_delivery(day: str, payload: ReviewSubmissionIn) -> SubmissionOut:
    parsed_day = _parse_day(day)
    if not payload.station.strip():
        raise HTTPException(status_code=422, detail="station must be non-empty")
    if not payload.submission_no.strip():
        raise HTTPException(status_code=422, detail="submission_no must be non-empty")

    notices = _build_notices(payload, parsed_day)
    svc = service()
    existing = svc.store.get_submission(parsed_day.isoformat(), payload.station.strip())
    try:
        record = svc.submit(
            parsed_day,
            payload.station,
            payload.submission_no,
            notices,
            payload.complete,
        )
    except DuplicateAnnouncement as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except SubmissionConflict as exc:
        raise HTTPException(status_code=409, detail={"kind": exc.kind, "error": exc.detail}) from exc
    accepted = not (
        existing is not None
        and existing.submission_no == record.submission_no
        and existing.content_hash == record.content_hash
    )
    return SubmissionOut.from_record(record, accepted=accepted)


@router.post("/days/{day}/evaluation", response_model=MinuteStateOut)
def evaluate_day(day: str, payload: EvaluateIn | None = None) -> MinuteStateOut:
    parsed_day = _parse_day(day)
    target = payload.minute if payload is not None else None
    svc = service()
    try:
        written = svc.evaluate(parsed_day, target)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if written:
        return MinuteStateOut.from_state(written[-1])
    state = svc.minute_state(parsed_day, target if target is not None else _clock_minute(svc, parsed_day))
    if state is None:
        raise HTTPException(
            status_code=404,
            detail="no minute has been evaluated yet for this day",
        )
    return MinuteStateOut.from_state(state)


def _clock_minute(svc: ReviewService, day) -> int:
    return svc.clock.current_minute(day)


@router.get("/days/{day}/minutes/{minute}", response_model=MinuteStateOut)
def minute_view(day: str, minute: int) -> MinuteStateOut:
    parsed_day = _parse_day(day)
    if not 0 <= minute <= 1439:
        raise HTTPException(status_code=422, detail="minute must be within 0..1439")
    state = service().minute_state(parsed_day, minute)
    if state is None:
        raise HTTPException(
            status_code=404,
            detail={"minute": minute, "error": "this minute has not been evaluated yet"},
        )
    return MinuteStateOut.from_state(state)


@router.get("/days/{day}/minutes", response_model=list[MinuteStateOut])
def day_timeline(day: str) -> list[MinuteStateOut]:
    parsed_day = _parse_day(day)
    return [MinuteStateOut.from_state(s) for s in service().timeline(parsed_day)]


@router.get("/numbers/{review_no}", response_model=ReviewServeOut)
def serve_review_number(review_no: str) -> ReviewServeOut:
    svc = service()
    try:
        record, latest = svc.serve(review_no)
    except ReviewNumberNotServable as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "review_no": exc.review_no,
                "error": exc.reason,
                "latest_status": exc.status,
            },
        ) from exc
    return ReviewServeOut.build(record, latest)
