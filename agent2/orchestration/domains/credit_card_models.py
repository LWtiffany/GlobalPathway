"""Credit-card domain contracts shared by repositories, scoring and the workflow."""

from datetime import date, datetime
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
    path_id: str = "hard"
    outcome_if_met: Literal["meets_public_criteria", "needs_bank_assessment"] = "meets_public_criteria"
    application_channel: str = "all"
    description: str | None = None
    source_url: str | None = None
    last_verified_at: datetime | None = None


class RewardRule(CardModel):
    rule_id: str
    spend_category: str
    reward_type: Literal["cashback", "points", "miles"]
    reward_rate: Decimal | None = None
    points_per_currency_unit: Decimal | None = None
    cash_equivalent_rate: Decimal | None = None
    spend_cap: Decimal | None = None
    cap_currency: str | None = None
    cap_period: str | None = None
    # "total": the rule states the full earn rate for its category (replaces base).
    # "extra": the rule adds on top of the card's base-rate layer (stackable).
    stacking: Literal["total", "extra"] = "total"
    # Promotional rules stop applying after this instant (e.g. Pulse QR 2%, Signature 1.2%).
    valid_until: datetime | None = None
    qualification_type: Literal[
        "automatic", "selected_category", "payment_method", "designated_merchant"
    ] = "automatic"
    qualification_value: str | None = None
    description: str | None = None
    source_url: str | None = None
    last_verified_at: datetime | None = None


class CardFee(CardModel):
    fee_id: str
    fee_type: str
    amount: Decimal | None = None
    currency: str | None = None
    rate: Decimal | None = None
    waiver_condition: str | None = None
    # Annual eligible-spend threshold that keeps this fee waived (e.g. EveryMile HKD80,000).
    waiver_min_annual_spend_hkd: Decimal | None = None
    notes: str | None = None
    scope_text: str | None = None
    source_url: str | None = None
    last_verified_at: datetime | None = None


class CardBenefit(CardModel):
    benefit_id: str
    benefit_type: str
    benefit_name: str
    quantity: Decimal | None = None
    period: str | None = None
    description: str | None = None
    source_url: str | None = None
    last_verified_at: datetime | None = None


class CardBorrowingTerm(CardModel):
    borrowing_term_id: str
    term_type: Literal[
        "purchase_apr", "cash_advance_apr", "interest_free_period",
        "cash_instalment_offer", "cash_instalment_example", "spending_instalment",
    ]
    apr: Decimal | None = None
    monthly_fee_rate: Decimal | None = None
    tenor_months: int | None = None
    max_interest_free_days: int | None = None
    example_principal: Decimal | None = None
    periodic_repayment: Decimal | None = None
    total_repayment: Decimal | None = None
    currency: str | None = None
    available: bool = True
    personalized_pricing: bool = False
    description: str
    source_url: str
    source_effective_date: date | None = None
    last_verified_at: datetime


class CreditCardProduct(CardModel):
    card_id: str
    issuing_bank: str
    card_network: str
    market: str
    product_name: str
    card_tier: str | None = None
    target_audience: str | None = None
    official_url: str | None = None
    last_verified_at: datetime | None = None
    eligibility_rules: list[EligibilityRule] = Field(default_factory=list)
    reward_rules: list[RewardRule] = Field(default_factory=list)
    fees: list[CardFee] = Field(default_factory=list)
    benefits: list[CardBenefit] = Field(default_factory=list)
    borrowing_terms: list[CardBorrowingTerm] = Field(default_factory=list)


class CardCatalogRead(CardModel):
    read_id: str
    market: str
    products: list[CreditCardProduct]
    source_total: int | None = None
    source_active: int | None = None
    source_filtered: int | None = None


class EligibilityAssessment(CardModel):
    status: Literal["eligible", "needs_bank_assessment", "unknown", "ineligible"]
    reasons: list[str] = Field(default_factory=list)
    missing_fields: list[str] = Field(default_factory=list)
    matched_path: str | None = None
    # True when the only failing written criterion is HK residency: HSBC Hong
    # Kong lists it as a basic requirement, yet a small number of non-resident
    # applications do get approved case by case. The card stays recommended
    # with a low-approval-chance disclosure instead of being excluded.
    low_approval_chance: bool = False


class CardRecommendation(CardModel):
    card_id: str
    product_name: str
    issuing_bank: str
    card_network: str
    role: Literal["primary", "supplement", "alternative"]
    eligibility: EligibilityAssessment
    score: Decimal
    score_breakdown: dict[str, Decimal] = Field(default_factory=dict)
    estimated_monthly_spend_hkd: Decimal | None = None
    estimated_annual_reward_hkd: Decimal | None = None
    estimated_annual_fee_hkd: Decimal | None = None
    # Annual fee after any introductory waiver period (year 3 onwards), used both
    # for tie-breaking and for honest long-run cost disclosure in the answer.
    ongoing_annual_fee_hkd: Decimal | None = None
    estimated_annual_fx_cost_hkd: Decimal | None = None
    estimated_annual_net_value_hkd: Decimal | None = None
    reward_units: dict[str, Decimal] = Field(default_factory=dict)
    coverage_weight: Decimal = Decimal("0")
    benefit_fit: Decimal = Decimal("0")
    data_confidence: Decimal = Decimal("0")
    matched_scenarios: list[str] = Field(default_factory=list)
    # Descriptions of promotional rules that actively contributed to the reward
    # estimate (e.g. Pulse QR 2%, Signature 1.2%), for honest time-limited disclosure.
    promo_rules: list[str] = Field(default_factory=list)
    annual_fee: Decimal | None = None
    annual_fee_currency: str | None = None
    annual_fee_waiver: str | None = None
    foreign_transaction_fee_rate: Decimal | None = None
    main_benefits: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    official_url: str | None = None
    purchase_apr: Decimal | None = None
    cash_advance_apr: Decimal | None = None
    max_interest_free_days: int | None = None
    borrowing_sources: list[str] = Field(default_factory=list)


class CreditCardRecommendationSet(CardModel):
    primary: CardRecommendation | None = None
    supplements: list[CardRecommendation] = Field(default_factory=list)
    alternatives: list[CardRecommendation] = Field(default_factory=list)
    ranked_candidates: list[CardRecommendation] = Field(default_factory=list)
    excluded: dict[str, list[str]] = Field(default_factory=dict)
