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
    content: str = Field(min_length=1, max_length=50_000)
    content_type: Literal["text", "markdown"] = "text"
    web_access_allowed: bool = False


class SourceUrlCreate(ApiModel):
    url: str = Field(min_length=8, max_length=2_048)
    title: str | None = Field(default=None, min_length=1, max_length=300)
    learning_goal: str = Field(min_length=1, max_length=500)
    web_access_allowed: bool = False


class SourceResolveCreate(ApiModel):
    input: str = Field(min_length=1, max_length=50_000)
    title: str | None = Field(default=None, min_length=1, max_length=300)
    learning_goal: str = Field(min_length=1, max_length=500)
    content_type: Literal["text", "markdown"] = "text"
    web_access_allowed: bool = False


class SourceRead(ApiModel):
    id: UUID
    title: str
    learning_goal: str
    content_type: str
    origin_type: str
    origin_url: str | None
    retrieved_at: datetime | None = None
    origin_content_hash: str | None
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
    retrieved_at: datetime | None = None


class DraftUnitRead(ApiModel):
    id: UUID
    position: int
    status: str = "active"
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
    title: str | None = None
    learning_goal: str
    agent_summary: dict[str, Any]
    confirmed_at: datetime | None
    created_at: datetime
    units: list[DraftUnitRead]


class KnowledgeSetSourceRead(ApiModel):
    id: UUID
    title: str
    origin_type: str
    origin_url: str | None = None
    context_type: Literal["url", "conversation", "direct_input"]


class KnowledgeSetSummaryRead(ApiModel):
    id: UUID
    title: str
    unit_count: int
    due_count: int
    review_count: int
    last_reviewed_at: datetime | None = None
    source: KnowledgeSetSourceRead
    created_at: datetime
    updated_at: datetime


class KnowledgeUnitManageRead(ApiModel):
    id: UUID
    position: int
    title: str
    question: str
    answer: str
    explanation: str
    evidence: list[EvidenceRead]
    review_count: int
    last_reviewed_at: datetime | None = None


class KnowledgeSetDetailRead(KnowledgeSetSummaryRead):
    learning_goal: str
    units: list[KnowledgeUnitManageRead]


class KnowledgeSetUpdate(ApiModel):
    title: str = Field(min_length=1, max_length=300)


class KnowledgeUnitUpdate(ApiModel):
    question: str = Field(min_length=1, max_length=4_000)
    answer: str = Field(min_length=1, max_length=8_000)


class ConversationRead(ApiModel):
    id: UUID
    title: str
    title_status: str
    turn_count: int
    created_at: datetime
    updated_at: datetime


class ConversationUpdate(ApiModel):
    title: str = Field(min_length=1, max_length=100)


class ConversationTurnCreate(SourceResolveCreate):
    learning_goal: str = Field(
        default="准确整理并记住这份材料",
        min_length=1,
        max_length=500,
    )
    idempotency_key: str = Field(min_length=8, max_length=128)


class ConversationTurnRead(ApiModel):
    id: UUID
    position: int
    user_content: str
    source_id: UUID | None
    source_type: str | None = None
    source_title: str | None = None
    source_url: str | None = None
    run_id: UUID | None
    run_state: str | None
    assistant_summary: str | None
    draft: DraftRead | None
    created_at: datetime


class ConversationDetailRead(ApiModel):
    conversation: ConversationRead
    turns: list[ConversationTurnRead]


class ConversationTurnHistoryRead(ApiModel):
    items: list[ConversationTurnRead]
    next_after_position: int | None


class ConversationTurnStartRead(ApiModel):
    conversation: ConversationRead
    turn: ConversationTurnRead
    run: RunRead


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
    evaluation_status: Literal["disabled", "pending", "completed", "failed"]
    evaluation: dict[str, Any] | None = None
    submitted_at: datetime


class ReviewEvaluationRead(ApiModel):
    attempt_id: UUID
    card_id: UUID
    status: Literal["disabled", "pending", "completed", "failed"]
    evaluation: dict[str, Any] | None = None


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
    ai_suggested_rating: Literal[1, 2, 3, 4] | None = None
    user_overrode_ai: bool | None = None


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
    ai_evaluation_enabled: bool = True
    timezone: str = Field(default="Asia/Shanghai", min_length=1, max_length=64)


class ReminderPreferenceRead(ReminderPreferenceUpdate):
    id: UUID
    user_id: UUID
    created_at: datetime
    updated_at: datetime


class ReminderSubscriptionGrantCreate(ApiModel):
    template_id: str = Field(min_length=1, max_length=128)
    result: Literal["accept", "acceptWithAudio", "reject", "ban", "filter"]
    idempotency_key: str = Field(min_length=8, max_length=128)


class ReminderSubscriptionStatusRead(ApiModel):
    template_id: str | None
    delivery_enabled: bool
    available_grants: int
    last_delivery_status: str | None
    last_sent_at: datetime | None


class ReminderDispatchClaim(ApiModel):
    batch_size: int = Field(default=50, ge=1, le=200)
    lease_seconds: int = Field(default=120, ge=30, le=600)


class ReminderDispatchResult(ApiModel):
    status: Literal["sent", "failed", "uncertain"]
    wechat_errcode: int | None = None
    wechat_errmsg: str | None = Field(default=None, max_length=500)
    response: dict[str, Any] | None = None
