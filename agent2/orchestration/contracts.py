"""Stable contracts between planning, domains, gates, composition and persistence."""

from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from agent2.models import (AnswerBody, AnswerStatus, Citation, Claim, Conflict,
                           FollowUpQuestion, Notice)

Domain = Literal["visa", "fx", "bank_account", "credit_card"]


class FrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True)


class DomainTask(FrozenModel):
    task_id: str
    domain: Domain
    intent: str
    facts: dict[str, Any] = Field(default_factory=dict)
    basis: dict[str, str] = Field(default_factory=dict)
    depends_on: list[str] = Field(default_factory=list)
    confidence: float = 1.0
    source: Literal["rules", "llm", "session"] = "rules"


class IntentHit(FrozenModel):
    intent: str
    source: Literal["rules", "llm", "session"]
    confidence: float
    evidence: str | None = None


class RoutingAudit(FrozenModel):
    """Why this turn routed to specific modules (rules ∪ LLM ∪ session)."""

    rules_hits: list[IntentHit] = Field(default_factory=list)
    llm_intents: list[IntentHit] = Field(default_factory=list)
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
    observed_facts: dict[str, Any] = Field(default_factory=dict)
    observed_basis: dict[str, str] = Field(default_factory=dict)
    knowledge_read_id: str | None = None
    model_version: str = "none"


class QualityReport(FrozenModel):
    results: list[DomainResult]
    gate_version: str
    findings: list[str] = Field(default_factory=list)


class UnifiedAnswer(FrozenModel):
    status: AnswerStatus
    answer: AnswerBody
    domain_results: list[DomainResult] = Field(default_factory=list)
    missing_fields: list[str] = Field(default_factory=list)
    next_actions: list[str] = Field(default_factory=list)
    notices: list[Notice] = Field(default_factory=list)
    follow_up_questions: list[FollowUpQuestion] = Field(default_factory=list)


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


class PlanItemCandidate(FrozenModel):
    text: str
    source_task_id: str


class LifecycleActions(FrozenModel):
    advice_updates: list[AdviceUpdate] = Field(default_factory=list)
    profile_change_candidates: list[ProfileChangeCandidate] = Field(default_factory=list)
    plan_item_candidates: list[PlanItemCandidate] = Field(default_factory=list)


class OrchestrationInput(FrozenModel):
    text: str
    profile_id: str
    profile_version: int
    profile_values: dict[str, Any] = Field(default_factory=dict)
    conversation_intents: list[str] = Field(default_factory=list)
    asked_fields: list[str] = Field(default_factory=list)
    input_mode: Literal["free_text", "structured_selection"] = "free_text"
    understood_facts: dict[str, Any] = Field(default_factory=dict)
    understood_basis: dict[str, str] = Field(default_factory=dict)
    # Service may run understanding once for routing; workflow skips a second call.
    understanding_applied: bool = False
    # Intents recovered by UserUnderstandingPort (distinct from session conversation_intents).
    llm_intents: list[str] = Field(default_factory=list)


class OrchestrationOutput(FrozenModel):
    trace_id: str
    answer: UnifiedAnswer
    snapshot: AnswerSnapshot
    lifecycle: LifecycleActions
    stage_ms: dict[str, float] = Field(default_factory=dict)
