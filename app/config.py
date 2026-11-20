"""Service configuration, read from the environment."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    """Runtime settings for the archive service."""

    db_path: Path
    source_base_url: str
    source_version: str
    source_timeout_seconds: float
    max_round_queries: int = 6
    page_size: int = 5
    manual_window_seconds: int = 60
    # When set, the review replay uses a clock pinned to this UTC minute instead
    # of the wall clock (demo/test only). Unset in normal operation.
    review_clock_minute: int | None = None

    @classmethod
    def from_env(cls) -> "Settings":
        pinned = os.environ.get("REVIEW_CLOCK_MINUTE")
        return cls(
            db_path=Path(os.environ.get("ARCHIVE_DB_PATH", "/data/archive.sqlite3")),
            source_base_url=os.environ.get("SOURCE_BASE_URL", "http://localhost:8081").rstrip("/"),
            source_version=os.environ.get("SOURCE_VERSION", "v1"),
            source_timeout_seconds=float(os.environ.get("SOURCE_TIMEOUT_SECONDS", "10")),
            max_round_queries=int(os.environ.get("MAX_ROUND_QUERIES", "6")),
            page_size=5,
            manual_window_seconds=60,
            review_clock_minute=int(pinned) if pinned not in (None, "") else None,
        )


settings = Settings.from_env()
