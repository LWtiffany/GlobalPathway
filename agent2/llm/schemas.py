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


class WrittenAnswer(StrictModel):
    summary: str = Field(min_length=1, max_length=500)
    steps: list[str] = Field(default_factory=list, max_length=12)
