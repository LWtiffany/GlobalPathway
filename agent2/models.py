"""Contracts shared by the Agent 2 modules (S1, docs/agent2-s1-uk-student-visa.md §4)."""
from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

AnswerStatus = Literal["answered", "needs_input", "needs_verification", "cannot_answer", "out_of_scope"]
ANSWER_STATUSES = ("answered", "needs_input", "needs_verification", "cannot_answer", "out_of_scope")


# ---------- M02 Profile ----------

class ProfileField(BaseModel):
    value: Any = None
    status: Literal["confirmed", "unknown"] = "unknown"
    source: Literal["user", "import", "demo"] | None = None
    confirmed_at: str | None = None


class Profile(BaseModel):
    profile_id: str
    user_id: str
    version: int
    synthetic: bool = False
    fields: dict[str, ProfileField] = Field(default_factory=dict)

    def confirmed(self, field):
        item = self.fields.get(field)
        return item.value if item and item.status == "confirmed" and item.value is not None else None


class ProfileChangeProposal(BaseModel):
    proposal_id: str
    profile_id: str
    field_code: str
    old_value: Any = None
    new_value: Any = None
    source_message_id: str | None = None
    basis: str
    status: Literal["open", "confirmed", "dismissed"] = "open"


# ---------- M07 Knowledge Read envelope ----------

class Condition(BaseModel):
    """One predicate on a Profile field; a requirement applies when all its conditions hold."""
    field: str
    op: Literal["eq", "ne", "lt", "lte", "gt", "gte", "in", "is_true", "is_false"]
    value: Any = None
    text: str


class Source(BaseModel):
    source_id: str
    name: str
    url: str
    authority: Literal["official", "third_party", "synthetic"]


class Requirement(BaseModel):
    requirement_key: str
    document_code: str
    name_zh: str
    name_en: str
    required: bool
    applicability_status: Literal["unconditional", "conditional", "unknown"]
    condition: list[Condition] | None = None
    # 任一组条件全部成立即适用；与 condition 二选一
    any_of: list[list[Condition]] | None = None
    evidence_id: str
    evidence_quote: str
    condition_quote: str | None = None
    # 证据所在页面；为空时用记录的 source
    source: Source | None = None

    def alternatives(self):
        return self.any_of or ([self.condition] if self.condition else [])

    def condition_text(self):
        return "，或".join("且".join(c.text for c in group) for group in self.alternatives()) or None


class KnowledgeRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    record_id: str
    revision_id: str
    schema_version: str = "agent2-knowledge-1"
    country: str
    visa_category: str
    topic: str
    status: Literal["ACTIVE", "PENDING_REVIEW", "WITHDRAWN"]
    synthetic: bool
    verified_at: datetime
    effective_from: date | None = None
    effective_to: date | None = None
    source: Source
    # 夹具记录自带快照供证据核对；Agent1 记录由发布方按快照 sha256 核对后给出可用性
    snapshot: str | None = None
    requirements: list[Requirement]


class ReadRecord(BaseModel):
    """A record as returned by KnowledgeRead, with its availability decided for `now`."""
    record: KnowledgeRecord
    available: bool
    availability_reason: Literal["available", "pending_review", "withdrawn", "superseded", "stale",
                                 "not_yet_effective", "expired", "missing_evidence"]


# ---------- M06 clarification ----------

class FollowUpQuestion(BaseModel):
    question_code: str
    field: str
    text: str
    rule: str
    profile_version: int


class Notice(BaseModel):
    code: str
    message: str


# ---------- M09 / M10 answer ----------

class Citation(BaseModel):
    citation_id: str
    record_id: str
    revision_id: str
    evidence_id: str
    source_name: str
    source_url: str | None
    quote: str
    condition_quote: str | None = None
    verified_at: datetime
    synthetic: bool


class Material(BaseModel):
    requirement_key: str
    document_code: str
    name_zh: str
    name_en: str
    applicability: Literal["required", "conditional", "unverified"]
    condition_text: str | None = None
    pending_fields: list[str] = Field(default_factory=list)
    citation_id: str


class Claim(BaseModel):
    claim_id: str
    text: str
    risk: Literal["high", "low"]
    citation_ids: list[str]
    requirement_key: str | None = None


class Conflict(BaseModel):
    document_code: str
    name_zh: str
    variants: list[dict]


class AnswerBody(BaseModel):
    summary: str
    steps: list[str] = Field(default_factory=list)
    materials: list[Material] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)


class AnswerResult(BaseModel):
    message_id: str
    status: AnswerStatus
    profile_id: str
    profile_version: int
    answer: AnswerBody
    claims: list[Claim] = Field(default_factory=list)
    citations: list[Citation] = Field(default_factory=list)
    conflicts: list[Conflict] = Field(default_factory=list)
    missing_fields: list[str] = Field(default_factory=list)
    follow_up_questions: list[FollowUpQuestion] = Field(default_factory=list)
    next_actions: list[str] = Field(default_factory=list)
    notices: list[Notice] = Field(default_factory=list)
    advice_updates: list[str] = Field(default_factory=list)
    profile_change_proposals: list[ProfileChangeProposal] = Field(default_factory=list)
    synthetic: bool = False
    trace_id: str
    stage_ms: dict[str, float] = Field(default_factory=dict)
