"""Credit-card domain contracts shared by repositories, scoring and the workflow."""

from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class CardModel(BaseModel):
    model_config = ConfigDict(frozen=True)


class EligibilityRule(CardModel):
    rule_id: str
    rule_type: str
    operator: str
    value_text: str | None = None
    value_numeric: Decimal | None = None
    currency: str | None = None
    required: bool = True
    description: str | None = None


class RewardRule(CardModel):
    rule_id: str
    spend_category: str
    reward_type: Literal["cashback", "points", "miles"]
    reward_rate: Decimal | None = None
    points_per_currency_unit: Decimal | None = None
    spend_cap: Decimal | None = None
    cap_currency: str | None = None
    cap_period: str | None = None
    description: str | None = None


class CardFee(CardModel):
    fee_id: str
    fee_type: str
    amount: Decimal | None = None
    currency: str | None = None
    rate: Decimal | None = None
    waiver_condition: str | None = None
    notes: str | None = None


class CardBenefit(CardModel):
    benefit_id: str
    benefit_type: str
    benefit_name: str
    quantity: Decimal | None = None
    period: str | None = None
    description: str | None = None


class CreditCardProduct(CardModel):
    card_id: str
    issuing_bank: str
    card_network: str
    market: str
    product_name: str
    card_tier: str | None = None
    target_audience: str | None = None
    official_url: str | None = None
    data_status: Literal["verified", "estimated", "synthetic"]
    last_verified_at: datetime | None = None
    eligibility_rules: list[EligibilityRule] = Field(default_factory=list)
    reward_rules: list[RewardRule] = Field(default_factory=list)
    fees: list[CardFee] = Field(default_factory=list)
    benefits: list[CardBenefit] = Field(default_factory=list)


class CardCatalogRead(CardModel):
    read_id: str
    market: str
    products: list[CreditCardProduct]


class EligibilityAssessment(CardModel):
    status: Literal["eligible", "unknown", "ineligible"]
    reasons: list[str] = Field(default_factory=list)
    missing_fields: list[str] = Field(default_factory=list)


class CardRecommendation(CardModel):
    card_id: str
    product_name: str
    issuing_bank: str
    card_network: str
    role: Literal["primary", "supplement", "alternative"]
    eligibility: EligibilityAssessment
    score: Decimal
    score_breakdown: dict[str, Decimal] = Field(default_factory=dict)
    matched_scenarios: list[str] = Field(default_factory=list)
    annual_fee: Decimal | None = None
    annual_fee_currency: str | None = None
    annual_fee_waiver: str | None = None
    foreign_transaction_fee_rate: Decimal | None = None
    main_benefits: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    official_url: str | None = None
    data_status: str


class CreditCardRecommendationSet(CardModel):
    primary: CardRecommendation | None = None
    supplements: list[CardRecommendation] = Field(default_factory=list)
    alternatives: list[CardRecommendation] = Field(default_factory=list)
    excluded: dict[str, list[str]] = Field(default_factory=dict)
