"""Stable contracts between planning, domains, gates, composition and persistence."""

from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from agent2.models import (AnswerBody, AnswerStatus, Citation, Claim, Conflict,
                           FollowUpQuestion, Notice)

Domain = Literal["visa", "fx", "bank_account", "credit_card"]
CapabilityOperation = Literal[
    "recommend", "explain", "compare", "assess", "how_to", "troubleshoot",
    "list_options", "request_alternatives", "focus", "select", "reject", "correct",
]


class FrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True)


class EntityMention(FrozenModel):
    text: str
    entity_type: str | None = None
    resolved_id: str | None = None
    confidence: float = Field(default=1.0, ge=0, le=1)


class CapabilityAction(FrozenModel):
    action_id: str
    operation: CapabilityOperation
    aspects: list[str] = Field(default_factory=list)
    entity_mentions: list[EntityMention] = Field(default_factory=list)
    confidence: float = Field(default=1.0, ge=0, le=1)
    evidence: str | None = None


class DomainTask(FrozenModel):
    task_id: str
    domain: Domain
    intent: str
    facts: dict[str, Any] = Field(default_factory=dict)
    basis: dict[str, str] = Field(default_factory=dict)
    depends_on: list[str] = Field(default_factory=list)
    confidence: float = 1.0
    source: Literal["rules", "llm", "session"] = "rules"
    actions: list[CapabilityAction] = Field(default_factory=list)


class IntentHit(FrozenModel):
    intent: str
    source: Literal["rules", "llm", "session"]
    confidence: float
    evidence: str | None = None


class RoutingAudit(FrozenModel):
    """Why this turn routed to specific modules (rules ∪ LLM ∪ session)."""

    rules_hits: list[IntentHit] = Field(default_factory=list)
    llm_intents: list[IntentHit] = Field(default_factory=list)
    dropped_llm_intents: list[str] = Field(default_factory=list)
    session_intents: list[str] = Field(default_factory=list)
    selected_intents: list[str] = Field(default_factory=list)
    unsupported_intents: list[str] = Field(default_factory=list)
    refusal_reason: str | None = None


class TaskPlan(FrozenModel):
    tasks: list[DomainTask] = Field(default_factory=list)
    unsupported_intents: list[str] = Field(default_factory=list)
    planner_version: str
    routing_audit: RoutingAudit | None = None


class DomainResult(FrozenModel):
    task_id: str
    domain: Domain
    status: AnswerStatus
    answer: AnswerBody
    claims: list[Claim] = Field(default_factory=list)
    citations: list[Citation] = Field(default_factory=list)
    conflicts: list[Conflict] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list)
    missing_fields: list[str] = Field(default_factory=list)
    next_actions: list[str] = Field(default_factory=list)
    notices: list[Notice] = Field(default_factory=list)
    follow_up_questions: list[FollowUpQuestion] = Field(default_factory=list)
    payload: dict[str, Any] = Field(default_factory=dict)
    # recommendation results may update the durable plan card; chat-only Q&A
    # answers a question without replacing the user's saved recommendation.
    projection: Literal["advice", "chat_only"] = "advice"
    observed_facts: dict[str, Any] = Field(default_factory=dict)
    observed_basis: dict[str, str] = Field(default_factory=dict)
    knowledge_read_id: str | None = None
    model_version: str = "none"


class QualityReport(FrozenModel):
    results: list[DomainResult]
    gate_version: str
    findings: list[str] = Field(default_factory=list)


class JourneyStep(FrozenModel):
    order: int
    task_id: str
    domain: Domain
    label: str
    guidance: str
    can_run_in_parallel: bool = False


class UnifiedAnswer(FrozenModel):
    status: AnswerStatus
    answer: AnswerBody
    domain_results: list[DomainResult] = Field(default_factory=list)
    missing_fields: list[str] = Field(default_factory=list)
    next_actions: list[str] = Field(default_factory=list)
    notices: list[Notice] = Field(default_factory=list)
    follow_up_questions: list[FollowUpQuestion] = Field(default_factory=list)
    journey: list[JourneyStep] = Field(default_factory=list)


class AnswerSnapshot(FrozenModel):
    """Immutable audit snapshot created before projections are updated."""

    snapshot_version: str = "agent2-answer-snapshot-1"
    trace_id: str
    profile_id: str
    profile_version: int
    user_text: str
    task_plan: TaskPlan
    answer: UnifiedAnswer
    gate_version: str
    knowledge_read_ids: dict[str, str] = Field(default_factory=dict)
    model_versions: dict[str, str] = Field(default_factory=dict)
    prompt_versions: dict[str, str] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class AdviceUpdate(FrozenModel):
    domain: Domain
    topic: str
    source_task_id: str
    payload: dict[str, Any]


class ProfileChangeCandidate(FrozenModel):
    field_code: str
    old_value: Any = None
    new_value: Any
    basis: str


class ProfileDelta(FrozenModel):
    field_code: str
    operation: Literal["replace", "add", "remove", "clear"]
    value: Any = None
    subject: str = "profile_subject"
    expected_field_version: int | None = None
    confidence: float = Field(default=1.0, ge=0, le=1)
    evidence: str


class PlanItemCandidate(FrozenModel):
    text: str
    source_task_id: str


class LifecycleActions(FrozenModel):
    advice_updates: list[AdviceUpdate] = Field(default_factory=list)
    profile_change_candidates: list[ProfileChangeCandidate] = Field(default_factory=list)
    plan_item_candidates: list[PlanItemCandidate] = Field(default_factory=list)
    suppressed_effects: list[str] = Field(default_factory=list)


class TurnUnderstanding(FrozenModel):
    """Stable boundary between language understanding and code-owned effects."""
    actions: list[CapabilityAction] = Field(default_factory=list)
    profile_deltas: list[ProfileDelta] = Field(default_factory=list)
    topic_switch: bool = False
    schema_version: str = "turn-understanding-1"


class OrchestrationInput(FrozenModel):
    text: str
    profile_id: str
    profile_version: int
    profile_values: dict[str, Any] = Field(default_factory=dict)
    conversation_intents: list[str] = Field(default_factory=list)
    asked_fields: list[str] = Field(default_factory=list)
    input_mode: Literal["free_text", "structured_selection"] = "free_text"
    # Recompute saved advice from the current Profile without starting another interview.
    refresh_mode: bool = False
    understood_facts: dict[str, Any] = Field(default_factory=dict)
    understood_basis: dict[str, str] = Field(default_factory=dict)
    # Service may run understanding once for routing; workflow skips a second call.
    understanding_applied: bool = False
    # True only when service actually called the model for this turn.
    understanding_model_used: bool = False
    # Intents recovered by UserUnderstandingPort (distinct from session conversation_intents).
    llm_intents: list[str] = Field(default_factory=list)
    # Display name of the profile under advice; used to personalise follow-up copy.
    profile_name: str = ""
    # Confirmed role of the profile owner ("student" or "parent"); steers the
    # writer's point of view (address the parent vs the applicant).
    profile_role: str = ""
    # Dialogue gist so routing can tell continuations of the open topic from new topics.
    session_topic: str | None = None
    session_last_user_text: str = ""
    session_last_assistant_text: str = ""
    session_pending_questions: list[str] = Field(default_factory=list)
    # Understanding's explicit verdict that the user left the open session topic.
    llm_topic_switch: bool = False
    # Profile version the conversation last saw; a mismatch means the profile
    # was edited outside the chat and domains must explain the re-judgement.
    expected_profile_version: int | None = None
    # Ephemeral, conversation-scoped domain memory.  This is deliberately not
    # merged into Profile because focus/selection/rejection are dialogue state.
    domain_state: dict[str, Any] = Field(default_factory=dict)


class OrchestrationOutput(FrozenModel):
    trace_id: str
    answer: UnifiedAnswer
    snapshot: AnswerSnapshot
    lifecycle: LifecycleActions
    stage_ms: dict[str, float] = Field(default_factory=dict)
