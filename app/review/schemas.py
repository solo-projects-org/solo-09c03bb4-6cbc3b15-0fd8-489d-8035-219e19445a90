"""HTTP schemas for the review submission and verified-serving API."""

from __future__ import annotations

from pydantic import BaseModel, Field

from common.times import minute_label
from app.review.models import MinuteState, ReviewNumberRecord, SubmissionRecord


class NoticeIn(BaseModel):
    announcement_no: str = Field(description="公告编号", examples=["HB-EIA-2026-1119-01"])
    title: str = Field(description="公告标题", examples=["云岭风电场二期工程环境影响报告书公示"])
    published_at: str = Field(
        description="发布时间（ISO-8601，UTC）",
        examples=["2026-11-19T08:00:00Z"],
    )


class ReviewSubmissionIn(BaseModel):
    station: str = Field(
        description="归档站点标识（固定三个归档点之一）",
        examples=["station-a"],
    )
    submission_no: str = Field(
        description="交件号；同站同日按自然序比较，旧号拒收",
        examples=["S-001"],
    )
    complete: bool = Field(
        default=True,
        description="该站是否已确认当日清单收齐（空清单收齐也为 true）",
        examples=[True],
    )
    notices: list[NoticeIn] = Field(
        default_factory=list,
        description="按日查档 JSON 的公告清单；行序不影响比对",
    )

    model_config = {
        "json_schema_extra": {
            "example": {
                "station": "station-a",
                "submission_no": "S-001",
                "complete": True,
                "notices": [
                    {
                        "announcement_no": "HB-EIA-2026-1119-01",
                        "title": "云岭风电场二期工程环境影响报告书公示",
                        "published_at": "2026-11-19T08:00:00Z",
                    }
                ],
            }
        }
    }


class ReviewNoticeOut(BaseModel):
    announcement_no: str
    title: str
    published_at: str


class SubmissionOut(BaseModel):
    day: str
    station: str
    submission_no: str
    content_hash: str
    complete: bool
    notice_count: int
    accepted: bool = Field(description="true=本次受理；false=原样重送，未重复受理")
    created_at: str

    @classmethod
    def from_record(
        cls, record: SubmissionRecord, accepted: bool
    ) -> "SubmissionOut":
        return cls(
            day=record.day,
            station=record.station,
            submission_no=record.submission_no,
            content_hash=record.content_hash,
            complete=record.complete,
            notice_count=len(record.notices),
            accepted=accepted,
            created_at=record.created_at,
        )


class MinuteStateOut(BaseModel):
    day: str
    minute: int
    minute_label: str
    status: str = Field(
        description="active=已发复核号；insufficient_evidence=证据不足；disagreement=有分歧"
    )
    review_no: str | None
    content_hash: str | None
    supporting_stations: list[str]
    detail: str | None
    evaluated_at: str

    @classmethod
    def from_state(cls, state: MinuteState) -> "MinuteStateOut":
        return cls(
            day=state.day,
            minute=state.minute,
            minute_label=minute_label(state.minute),
            status=state.status,
            review_no=state.review_no,
            content_hash=state.content_hash,
            supporting_stations=list(state.supporting_stations),
            detail=state.detail,
            evaluated_at=state.evaluated_at,
        )


class EvaluateIn(BaseModel):
    minute: int | None = Field(
        default=None,
        ge=0,
        le=1439,
        description="评估到该分钟（含）；缺省由服务时钟决定",
        examples=[600],
    )


class ReviewServeOut(BaseModel):
    review_no: str
    day: str
    status: str
    supporting_stations: list[str]
    content_hash: str
    notices: list[ReviewNoticeOut]
    first_minute: int
    first_minute_label: str
    issued_at: str
    latest_minute: int
    latest_minute_label: str

    @classmethod
    def build(
        cls, record: ReviewNumberRecord, latest: MinuteState
    ) -> "ReviewServeOut":
        return cls(
            review_no=record.review_no,
            day=record.day,
            status=latest.status,
            supporting_stations=list(latest.supporting_stations),
            content_hash=record.content_hash,
            notices=[
                ReviewNoticeOut(**notice.as_dict()) for notice in record.notices
            ],
            first_minute=record.first_minute,
            first_minute_label=minute_label(record.first_minute),
            issued_at=record.issued_at,
            latest_minute=latest.minute,
            latest_minute_label=minute_label(latest.minute),
        )
