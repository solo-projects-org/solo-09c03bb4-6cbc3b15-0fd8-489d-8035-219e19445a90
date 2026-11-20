"""Domain models for the cross-station review flow.

A *submission* is one archive point's (station's) whole-day JSON delivery for a
UTC day, identified by a client-chosen submission number.  A *minute state* is
the per-minute cross-station verdict recorded while replaying the day minute by
minute.  A *review number* is issued for a canonical notice list once at least
two stations confirm the same list.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

MinuteStatus = Literal["active", "insufficient_evidence", "disagreement"]


@dataclass(frozen=True)
class ReviewNotice:
    """One notice triple: the fields that two stations must agree on."""

    announcement_no: str
    title: str
    published_at: str

    def as_dict(self) -> dict[str, str]:
        return {
            "announcement_no": self.announcement_no,
            "title": self.title,
            "published_at": self.published_at,
        }


@dataclass(frozen=True)
class SubmissionRecord:
    day: str
    station: str
    submission_no: str
    content_hash: str
    complete: bool
    notices: tuple[ReviewNotice, ...]
    created_at: str


@dataclass(frozen=True)
class MinuteState:
    day: str
    minute: int
    status: MinuteStatus
    review_no: str | None
    content_hash: str | None
    supporting_stations: tuple[str, ...]
    detail: str | None
    evaluated_at: str


@dataclass(frozen=True)
class ReviewNumberRecord:
    review_no: str
    day: str
    content_hash: str
    notices: tuple[ReviewNotice, ...]
    first_minute: int
    issued_at: str


class SubmissionConflict(RuntimeError):
    """A delivery breaks the submission-number ordering rules.

    ``kind`` is ``"stale_no"`` (older number than the station's latest) or
    ``"changed_payload"`` (same number resubmitted with different content).
    """

    def __init__(self, kind: str, detail: str) -> None:
        super().__init__(detail)
        self.kind = kind
        self.detail = detail


class ReviewNumberNotServable(RuntimeError):
    """A review number exists but is no longer (or not yet) valid for serving."""

    def __init__(self, review_no: str, reason: str, status: str | None) -> None:
        super().__init__(reason)
        self.review_no = review_no
        self.reason = reason
        self.status = status
