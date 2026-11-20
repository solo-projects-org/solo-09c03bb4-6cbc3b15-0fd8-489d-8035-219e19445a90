"""Canonical list hashing and submission-number ordering helpers.

Two submissions describe the same list when their *set* of notice triples
(announcement number, title, published time) is identical — row order must not
affect comparison.  Notices are canonicalised (stripped, sorted by
announcement number and deduplicated) and the canonical JSON is hashed with
SHA-256 so identical lists share one ``content_hash``.
"""

from __future__ import annotations

import hashlib
import json

from common.versions import compare_versions

from app.review.models import ReviewNotice


class DuplicateAnnouncement(ValueError):
    """A delivery lists the same announcement number twice."""


def canonical_notices(
    notices: list[ReviewNotice] | tuple[ReviewNotice, ...],
) -> tuple[ReviewNotice, ...]:
    """Strip fields, reject duplicate numbers and sort by announcement number."""
    cleaned: list[ReviewNotice] = []
    seen: set[str] = set()
    for notice in notices:
        no = notice.announcement_no.strip()
        title = notice.title.strip()
        published_at = notice.published_at.strip()
        if no in seen:
            raise DuplicateAnnouncement(
                f"duplicate announcement_no {no!r} within one delivery"
            )
        seen.add(no)
        cleaned.append(ReviewNotice(no, title, published_at))
    cleaned.sort(key=lambda n: n.announcement_no)
    return tuple(cleaned)


def canonical_json(notices: tuple[ReviewNotice, ...]) -> str:
    """Deterministic JSON for a canonical notice tuple."""
    return json.dumps(
        [notice.as_dict() for notice in notices],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def hash_notices(notices: tuple[ReviewNotice, ...]) -> str:
    """Stable content hash shared by every delivery of the same notice list."""
    digest = hashlib.sha256(canonical_json(notices).encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def is_older_submission_no(incoming: str, current: str) -> bool:
    """True when ``incoming`` is older than the station's latest number.

    Submission numbers are compared with the same natural ordering used for day
    versions (numeric chunks compare as numbers, text otherwise).
    """
    return compare_versions(incoming.strip(), current.strip()) < 0
