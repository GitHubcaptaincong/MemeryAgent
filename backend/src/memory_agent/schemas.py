from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class ApiModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class SourceCreate(ApiModel):
    title: str = Field(min_length=1, max_length=300)
    learning_goal: str = Field(min_length=1, max_length=500)
    content: str = Field(min_length=1, max_length=10_000)
    content_type: Literal["text", "markdown"] = "text"
    web_access_allowed: bool = False


class SourceRead(ApiModel):
    id: UUID
    title: str
    learning_goal: str
    content_type: str
    char_count: int
    web_access_allowed: bool
    status: str
    created_at: datetime


class RunCreate(ApiModel):
    source_id: UUID
    idempotency_key: str | None = Field(default=None, min_length=8, max_length=128)


class RunRead(ApiModel):
    id: UUID
    source_id: UUID
    state: str
    model_provider: str
    model_name: str | None
    tool_call_count: int
    web_search_count: int
    revision_count: int
    input_tokens: int
    output_tokens: int
    stop_reason: str | None
    error_code: str | None
    error_message: str | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


class EventRead(ApiModel):
    id: UUID
    run_id: UUID
    seq: int
    event_type: str
    payload: dict[str, Any]
    visible_to_user: bool
    created_at: datetime


class EvidenceRead(ApiModel):
    evidence_type: str
    source_id: UUID | None
    start_char: int | None
    end_char: int | None
    quote: str | None
    url: str | None


class DraftUnitRead(ApiModel):
    id: UUID
    position: int
    title: str
    learning_objective: str
    explanation: str
    key_points: list[Any]
    question: str
    answer_key: list[str]
    hints: list[Any]
    tags: list[str]
    applicable_scenarios: list[str]
    confidence: float
    requires_user_confirmation: bool
    uncertainties: list[str]
    evidence: list[EvidenceRead]


class DraftRead(ApiModel):
    id: UUID
    source_id: UUID
    run_id: UUID
    version: int
    status: str
    learning_goal: str
    agent_summary: dict[str, Any]
    confirmed_at: datetime | None
    created_at: datetime
    units: list[DraftUnitRead]


class DraftRevision(ApiModel):
    feedback: str = Field(min_length=1, max_length=2_000)


class MemoryCandidateRead(ApiModel):
    id: UUID
    run_id: UUID
    kind: str
    canonical_key: str
    content: str
    rationale: str
    importance: float
    confidence: float
    status: str
    created_at: datetime


class MemoryDecision(ApiModel):
    decision: Literal["approve", "reject"]


class HealthRead(ApiModel):
    status: str
    model_provider: str
    model_name: str
    database: str


class ReviewRatingOptionRead(ApiModel):
    rating: Literal[1, 2, 3, 4]
    due_at: datetime
    interval_days: float


class ReviewCardRead(ApiModel):
    id: UUID
    draft_unit_id: UUID
    title: str
    question: str
    hints: list[Any]
    source_title: str
    learning_goal: str
    due_at: datetime
    interval_days: float
    review_count: int
    lapse_count: int
    scheduler_version: str
    rating_options: list[ReviewRatingOptionRead]


class ReviewAnswerCreate(ApiModel):
    answer: str = Field(min_length=1, max_length=4_000)
    idempotency_key: str = Field(min_length=8, max_length=128)


class ReviewAnswerRead(ApiModel):
    attempt_id: UUID
    card_id: UUID
    answer: str
    answer_key: list[str]
    evidence: list[EvidenceRead]
    evaluation_status: Literal["self_rating_required"]
    submitted_at: datetime


class ReviewRatingCreate(ApiModel):
    attempt_id: UUID
    rating: Literal[1, 2, 3, 4]
    idempotency_key: str = Field(min_length=8, max_length=128)


class ReviewResultRead(ApiModel):
    card_id: UUID
    attempt_id: UUID
    rating: Literal[1, 2, 3, 4]
    reviewed_at: datetime
    next_due_at: datetime
    interval_days: float
    review_count: int
    lapse_count: int
    scheduler_version: str
    scheduler_state: dict[str, Any]
    schedule_before: dict[str, Any]
    user_rating_is_final: bool


class ReviewOverviewRead(ApiModel):
    due_count: int
    total_active: int
    next_due_at: datetime | None


class ReviewHistoryRead(ApiModel):
    id: UUID
    card_id: UUID
    title: str
    question: str
    source_title: str
    rating: Literal[1, 2, 3, 4]
    reviewed_at: datetime
    next_due_at: datetime
    interval_days: float
    scheduler_version: str
    user_rating_is_final: bool


class ReminderPreferenceUpdate(ApiModel):
    enabled: bool = True
    preferred_time: str = Field(default="20:00", pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")
    daily_limit: int = Field(default=10, ge=1, le=100)
    overdue_enabled: bool = True
    timezone: str = Field(default="Asia/Shanghai", min_length=1, max_length=64)


class ReminderPreferenceRead(ReminderPreferenceUpdate):
    id: UUID
    user_id: UUID
    created_at: datetime
    updated_at: datetime
