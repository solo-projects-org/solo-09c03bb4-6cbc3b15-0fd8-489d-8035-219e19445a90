"""Unit tests for canonical list hashing and submission-number ordering."""

from __future__ import annotations

import pytest

from app.review.canonical import (
    DuplicateAnnouncement,
    canonical_notices,
    canonical_json,
    hash_notices,
    is_older_submission_no,
)
from app.review.models import ReviewNotice


def notice(no: str, title: str = "标题", at: str = "2026-11-19T08:00:00Z") -> ReviewNotice:
    return ReviewNotice(no, title, at)


def test_order_does_not_change_the_hash() -> None:
    left = canonical_notices([notice("A"), notice("B"), notice("C")])
    right = canonical_notices([notice("C"), notice("A"), notice("B")])
    assert hash_notices(left) == hash_notices(right)


def test_any_field_change_changes_the_hash() -> None:
    base = canonical_notices([notice("A", "原标题"), notice("B")])
    new_title = canonical_notices([notice("A", "新标题"), notice("B")])
    new_time = canonical_notices(
        [notice("A", at="2026-11-19T09:00:00Z"), notice("B")]
    )
    new_no = canonical_notices([notice("A2"), notice("B")])
    base_hash = hash_notices(base)
    assert hash_notices(new_title) != base_hash
    assert hash_notices(new_time) != base_hash
    assert hash_notices(new_no) != base_hash


def test_empty_list_has_a_stable_hash() -> None:
    empty = canonical_notices([])
    assert hash_notices(empty) == hash_notices(canonical_notices([]))
    assert canonical_json(empty) == "[]"


def test_duplicate_announcement_no_is_rejected() -> None:
    with pytest.raises(DuplicateAnnouncement):
        canonical_notices([notice("A"), notice("A")])


def test_fields_are_stripped_before_hashing() -> None:
    padded = canonical_notices([notice("  A  ", "  标题  ")])
    clean = canonical_notices([notice("A", "标题")])
    assert hash_notices(padded) == hash_notices(clean)


def test_submission_no_natural_ordering() -> None:
    assert is_older_submission_no("S-9", "S-10")
    assert is_older_submission_no("S-001", "S-002")
    assert not is_older_submission_no("S-002", "S-001")
    assert not is_older_submission_no("S-001", "S-001")
    assert is_older_submission_no("2026-11-19.1", "2026-11-19.2")
