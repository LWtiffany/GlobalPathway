"""Strict schemas at the LLM boundary."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

SupportedIntent = Literal[
    "visa_documents", "remittance_tuition", "remittance_living",
    "overseas_spending", "hsbc_account", "credit_card",
]
FactValue = str | int | float | bool | list[str]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class FactCandidate(StrictModel):
    field_code: str
    value: FactValue
    confidence: float = Field(ge=0, le=1)
    evidence: str = Field(min_length=1)
    update_mode: Literal["replace", "add", "remove"] = "replace"


class UnderstandingResult(StrictModel):
    intents: list[SupportedIntent] = Field(default_factory=list)
    facts: list[FactCandidate] = Field(default_factory=list)
    topic_switch: bool = Field(
        default=False,
        description="True only when the user clearly leaves the open SESSION_CONTEXT topic for a different one.",
    )


class ConversationDecision(StrictModel):
    task_id: str
    operation: Literal["recommend", "explain", "compare", "assess", "how_to", "troubleshoot", "list_options", "request_alternatives", "focus"]
    evidence: str = Field(max_length=120)
    confidence: float = Field(ge=0, le=1)


class ConversationDecisions(StrictModel):
    decisions: list[ConversationDecision] = Field(default_factory=list)


class WrittenAnswer(StrictModel):
    summary: str = Field(min_length=1, max_length=500)
    steps: list[str] = Field(default_factory=list, max_length=12)


class CreditCardAdvisorDraft(StrictModel):
    analysis: str = Field(min_length=20, max_length=600)
    evidence_ids: list[str] = Field(min_length=1, max_length=8)
