"""Experimental HSBC account subintent classifier; not wired into production."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .contracts import StructuredModel
from .prompts import PromptRenderer

AccountSubintent = Literal[
    "account_necessity", "product_selection", "eligibility",
    "documents_process", "fees_balance", "application_issue",
    "external_remittance_tuition", "ambiguous_card",
]
AccountFactCode = Literal[
    "account_region", "account_product", "applicant_age", "account_applicant_relation",
    "uk_residency_years", "identity_document_type", "has_hk_identity_card",
    "tax_certificate_or_ssn", "application_location", "qualifying_course_offer",
    "guardian_present",
]


class AccountFact(BaseModel):
    model_config = ConfigDict(extra="forbid")

    field_code: AccountFactCode
    value: str | int | float | bool
    evidence: str = Field(min_length=1)


class AccountSubintentResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    primary_subintent: AccountSubintent
    subintents: list[AccountSubintent] = Field(min_length=1)
    facts: list[AccountFact] = Field(default_factory=list)

    @model_validator(mode="after")
    def check_labels(self):
        if len(self.subintents) != len(set(self.subintents)):
            raise ValueError("duplicate subintents")
        if self.primary_subintent not in self.subintents:
            raise ValueError("primary_subintent must be in subintents")
        return self


class AccountSubintentClassifier:
    prompt_version = "account-subintent-v2"

    def __init__(self, model: StructuredModel, renderer: PromptRenderer | None = None):
        self.model = model
        self.renderer = renderer or PromptRenderer()

    async def classify(self, text: str) -> AccountSubintentResult:
        prompt = self.renderer.render("account_subintent.jinja2", user_text=text)
        return await self.model.parse(
            prompt=prompt, output_type=AccountSubintentResult,
            operation="account_subintent",
        )
