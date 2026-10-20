"""Pure eligibility and ranking rules for credit-card recommendations."""

from decimal import Decimal
from functools import cmp_to_key

from .credit_card_models import (CardRecommendation, CreditCardProduct,
                                 CreditCardRecommendationSet, EligibilityAssessment)

SCENARIO_RULES = {
    "general": {"general"},
    # A selected-retail programme only applies to named merchants. It is not a
    # synonym for every online transaction.
    "online": {"online"},
    "dining": {"dining_and_lifestyle", "travel_and_dining"},
    "travel": {"travel_and_transport", "travel_and_dining", "overseas"},
    "overseas": {"overseas", "travel_and_transport", "travel_and_dining"},
    "mainland_china": {"mainland_china", "mainland_china_and_macau"},
    "tuition": {"tuition"},
    "retail": {"selected_retail"},
}

# HSBC Rewards of Your Choice offers one selected category.  The same 2.4% or
# 3.6% headline rate therefore cannot be applied to every selected scenario.
SELECTABLE_CATEGORY_SCENARIOS = {
    "dining", "travel", "overseas", "mainland_china", "retail",
}

MONTHLY_SPEND_ESTIMATES = {
    "under_2k": Decimal("1000"),
    "2k_5k": Decimal("3500"),
    "5k_10k": Decimal("7500"),
    "10k_20k": Decimal("15000"),
    "20k_plus": Decimal("30000"),
}


def _scenario_weights(scenarios: list[str]) -> list[Decimal]:
    """Weight selections in the order the user ranked them."""
    presets = {
        1: ("1",),
        2: ("0.60", "0.40"),
        3: ("0.50", "0.30", "0.20"),
    }
    raw = presets.get(len(scenarios))
    if raw:
        return [Decimal(item) for item in raw]
    if not scenarios:
        return []
    weight = Decimal("1") / Decimal(len(scenarios))
    return [weight] * len(scenarios)


def needs_asset_information(product: CreditCardProduct, facts: dict) -> bool:
    """Return true when a failed income route could be replaced by an asset route."""
    if any(facts.get(field) is not None for field in (
            "asset_band", "asset_proof_available", "provable_financial_assets_hkd")):
        return False
    asset_rules = [rule for rule in product.eligibility_rules
                   if rule.required and rule.rule_type in {"asset_proof", "minimum_assets"}]
    if not asset_rules:
        return False
    income_rules = [rule for rule in product.eligibility_rules
                    if rule.required and rule.rule_type == "minimum_income"
                    and rule.value_numeric is not None]
    if not income_rules:
        return True

    exact = facts.get("annual_income_hkd")
    if exact is not None:
        return all(Decimal(str(exact)) < rule.value_numeric for rule in income_rules)
    band = facts.get("annual_income_band_hkd")
    if not band:
        return False
    parts = band.split("_")
    if parts[0] == "lt":
        upper_exclusive = Decimal(parts[1])
        return all(upper_exclusive <= rule.value_numeric for rule in income_rules)
    if parts[0] == "range":
        upper = Decimal(parts[2])
        return all(upper < rule.value_numeric for rule in income_rules)
    return False


# HSBC Hong Kong lists HK residency as a written basic requirement, yet a
# small number of non-resident applications are approved case by case. A
# failed residency check therefore downgrades instead of excluding.
NON_RESIDENT_REASON = (
    "非香港居民：不符合成文条件（基本卡申请人须为香港特别行政区居民）；"
    "实践中仅有低几率个案通过，最终由银行审批。"
)


def assess_eligibility(product: CreditCardProduct, facts: dict) -> EligibilityAssessment:
    grouped = {}
    for rule in product.eligibility_rules:
        if rule.required:
            grouped.setdefault(rule.path_id, []).append(rule)

    def evaluate(rule):
        if rule.rule_type == "minimum_age":
            value, field = facts.get("applicant_age"), "applicant_age"
            band = facts.get("applicant_age_band")
            if value is not None:
                passed = Decimal(str(value)) >= rule.value_numeric
            elif band is not None and rule.value_numeric == 18:
                value, field, passed = band, "applicant_age_band", band == "18_or_over"
            else:
                passed = False
        elif rule.rule_type == "minimum_income":
            value, field = facts.get("annual_income_hkd"), "annual_income_hkd"
            band = facts.get("annual_income_band_hkd")
            if value is not None:
                passed = Decimal(str(value)) >= rule.value_numeric
            elif band is not None:
                value, field = band, "annual_income_band_hkd"
                parts = band.split("_")
                lower = Decimal(parts[1]) if parts[0] in ("gte", "range") else Decimal("0")
                upper = Decimal(parts[2]) if parts[0] == "range" else None
                passed = lower >= rule.value_numeric
                if not passed and upper is not None and upper >= rule.value_numeric:
                    value = None  # threshold falls inside a coarse band; keep eligibility unknown
            else:
                passed = False
        elif rule.rule_type == "minimum_assets":
            value, field = facts.get("provable_financial_assets_hkd"), "provable_financial_assets_hkd"
            if value is not None:
                passed = Decimal(str(value)) >= rule.value_numeric
            else:
                band, field = facts.get("asset_band"), "asset_band"
                bounds = {"none": (0, 0), "under_100k": (0, 99999),
                          "100k_399k": (100000, 399999), "400k_plus": (400000, None),
                          "500k_plus": (500000, None), "100k_500k": (100000, 500000)}
                value = band
                if band in bounds:
                    lower, upper = bounds[band]
                    passed = lower >= rule.value_numeric
                    if not passed and upper is not None and upper >= rule.value_numeric:
                        value = None
                else:
                    passed = False
        elif rule.rule_type == "asset_proof":
            value, field = facts.get("asset_proof_available"), "asset_proof_available"
            if value is None and facts.get("asset_band") is not None:
                value = facts["asset_band"] != "none"
            passed = value is True
        elif rule.rule_type == "student_status":
            value, field = facts.get("full_time_student"), "full_time_student"
            passed = value is True
        elif rule.rule_type == "institution_status":
            value, field = facts.get("student_institution_eligible"), "student_institution_eligible"
            passed = value is True
        elif rule.rule_type == "banking_relationship":
            path = facts.get("hsbc_premier_path")
            if path is not None:
                value, field = path, "hsbc_premier_path"
                passed = path == "existing_premier"
            else:
                value, field = facts.get("premier_relationship"), "premier_relationship"
                passed = value is True
        elif rule.rule_type == "relationship_balance":
            value, field = facts.get("hsbc_total_relationship_balance_hkd"), \
                "hsbc_total_relationship_balance_hkd"
            if value is not None:
                passed = Decimal(str(value)) >= rule.value_numeric
            else:
                path = facts.get("hsbc_premier_path")
                path_amounts = {"existing_premier": Decimal("100000"),
                                "can_qualify_with_1m_trb": Decimal("1000000"),
                                "not_applicable": Decimal("0")}
                band = facts.get("hsbc_relationship_balance_band")
                field = "hsbc_premier_path" if path is not None else \
                    "hsbc_relationship_balance_band"
                value = band
                lower_bounds = {"under_100k": Decimal("0"),
                                "100k_999k": Decimal("100000"),
                                "1m_plus": Decimal("1000000")}
                if path is not None:
                    value = path
                    passed = path_amounts[path] >= rule.value_numeric
                else:
                    passed = band in lower_bounds and lower_bounds[band] >= rule.value_numeric
        elif rule.rule_type == "residency":
            field = "hk_resident"
            value = facts.get(field)
            if value is None:
                legacy = facts.get("residency_status", facts.get("applicant_residence"))
                value = legacy
                passed = str(legacy).casefold() in {
                    "hong kong resident", "hong_kong_resident", "hk_resident", "hong kong", "hk"
                }
            else:
                passed = value is True
        else:
            field = {"residency": "residency_status", "credit_assessment": "credit_history_band"}.get(
                rule.rule_type, f"card_rule_{rule.rule_type}")
            value, passed = facts.get(field), False
        return "missing" if value is None else "passed" if passed else "failed", field

    hard_reasons, hard_missing = [], []
    resident_failed = False
    for rule in grouped.pop("hard", []):
        state, field = evaluate(rule)
        if state == "failed":
            if rule.rule_type == "residency":
                # Soft gate: keep the card recommended with a low-approval
                # disclosure instead of a hard exclusion.
                resident_failed = True
                hard_reasons.append(NON_RESIDENT_REASON)
            else:
                hard_reasons.append(f"不满足：{rule.description or rule.rule_type}")
        elif state == "missing":
            hard_missing.append(field)
            hard_reasons.append(f"缺少门槛信息：{rule.description or rule.rule_type}")
    if any(reason.startswith("不满足") for reason in hard_reasons):
        return EligibilityAssessment(status="ineligible", reasons=hard_reasons,
                                     missing_fields=list(dict.fromkeys(hard_missing)))
    if not grouped:
        status = "unknown" if hard_missing else \
            ("needs_bank_assessment" if resident_failed else "eligible")
        return EligibilityAssessment(
            status=status, reasons=hard_reasons,
            missing_fields=list(dict.fromkeys(hard_missing)), matched_path="hard",
            low_approval_chance=resident_failed)

    passed_paths, incomplete_paths = [], []
    failed_reasons = []
    for path_id, rules in grouped.items():
        results = [(rule, *evaluate(rule)) for rule in rules]
        if all(state == "passed" for _, state, _ in results):
            passed_paths.append((path_id, rules[0].outcome_if_met, rules))
        elif not any(state == "failed" for _, state, _ in results):
            incomplete_paths.append((path_id, results))
        else:
            failed_reasons.extend(
                f"不满足：{rule.description or rule.rule_type}"
                for rule, state, _ in results if state == "failed")

    public = next((item for item in passed_paths if item[1] == "meets_public_criteria"), None)
    review = next((item for item in passed_paths if item[1] == "needs_bank_assessment"), None)
    if hard_missing:
        status, matched = "unknown", None
    elif resident_failed and (public or review):
        # Residency is the only failing written criterion: downgrade the
        # passed path to case-by-case review instead of a hard exclusion.
        passed = public or review
        status, matched = "needs_bank_assessment", passed[0]
    elif public:
        status, matched = "eligible", public[0]
    elif review:
        status, matched = "needs_bank_assessment", review[0]
    elif incomplete_paths:
        status, matched = "unknown", None
    else:
        status, matched = "ineligible", None

    missing = list(hard_missing)
    # Once one OR eligibility path has passed, missing fields from unused
    # alternative paths (for example asset proof after income passed) are not
    # outstanding requirements and must not appear in the recommendation.
    if status not in {"eligible", "needs_bank_assessment"}:
        for _, results in incomplete_paths:
            missing.extend(field for _, state, field in results if state == "missing")
    if status == "eligible" and public:
        path_reasons = [f"满足：{rule.description or rule.rule_type}" for rule in public[2]]
        reasons = hard_reasons + path_reasons
    elif status == "needs_bank_assessment" and resident_failed and (public or review):
        passed = public or review
        prefix = "满足" if passed is public else "满足替代路径"
        path_reasons = [f"{prefix}：{rule.description or rule.rule_type}" for rule in passed[2]]
        reasons = hard_reasons + failed_reasons + path_reasons
    elif status == "needs_bank_assessment" and review:
        path_reasons = [f"满足替代路径：{rule.description or rule.rule_type}" for rule in review[2]]
        reasons = hard_reasons + failed_reasons + path_reasons + [
            "可提交资产证明申请，最终由HSBC个案审批。"
        ]
    else:
        reasons = hard_reasons + failed_reasons
    return EligibilityAssessment(status=status, reasons=list(dict.fromkeys(reasons)),
                                 missing_fields=list(dict.fromkeys(missing)), matched_path=matched,
                                 low_approval_chance=resident_failed)


def _fee(product, fee_type):
    return next((fee for fee in product.fees if fee.fee_type == fee_type), None)


def _borrowing_term(product, term_type):
    return next((term for term in product.borrowing_terms if term.term_type == term_type), None)


def _rule_qualifies(rule, scenario, facts, selected_category_scenario):
    if rule.qualification_type == "automatic":
        return True
    if rule.qualification_type == "selected_category":
        return scenario == selected_category_scenario
    if rule.qualification_type == "payment_method":
        return facts.get("mainland_payment_method") == rule.qualification_value
    if rule.qualification_type == "designated_merchant":
        usage = facts.get("designated_merchant_usage")
        return usage is True or usage == rule.qualification_value or (
            isinstance(usage, list) and rule.qualification_value in usage)
    return False


def _selected_category_scenario(product, scenarios, facts):
    if not any(rule.qualification_type == "selected_category"
               for rule in product.reward_rules):
        return None
    # A combined mainland mobile/QR headline rate already assumes the selected
    # Overseas and Mainland China category, so reserve the one category there.
    if ("mainland_china" in scenarios
            and facts.get("mainland_payment_method") == "mobile_or_qr"
            and any(rule.qualification_type == "payment_method"
                    and rule.qualification_value == "mobile_or_qr"
                    for rule in product.reward_rules)):
        return "mainland_china"
    return next((scenario for scenario in scenarios
                 if scenario in SELECTABLE_CATEGORY_SCENARIOS), None)


def _reward_value(rule):
    return (rule.cash_equivalent_rate or rule.reward_rate
            or rule.points_per_currency_unit or Decimal("0"))


def _annual_value_within_cap(annual_spend: Decimal, cap: Decimal | None) -> Decimal:
    return annual_spend if cap is None else min(annual_spend, cap)


def _annual_reward_metrics(product: CreditCardProduct, scenarios: list[str],
                           monthly_spend: Decimal, facts: dict, now=None):
    """Layered annual reward: one total rule per scenario plus stackable extras.

    HSBC reward structures are additive: a base rate on all spending, one
    selected-category extra (Rewards of Your Choice, capped per programme
    year) and promotional extras (e.g. Pulse QR 2%), each with its own cap.
    Spending above a layer cap keeps earning the layers beneath it; expired
    promotional rules (valid_until in the past) contribute nothing.
    """
    annual_cash = Decimal("0")
    units: dict[str, Decimal] = {}
    matched: list[str] = []
    promo_rules: list[str] = []
    weights = _scenario_weights(scenarios)
    selected_category_scenario = _selected_category_scenario(product, scenarios, facts)
    base_rules = [rule for rule in product.reward_rules
                  if rule.spend_category == "general"
                  and rule.qualification_type == "automatic"
                  and _rule_active(rule, now)]
    base_rule = max(base_rules, key=_reward_value) if base_rules else None
    for scenario, weight in zip(scenarios, weights):
        annual_spend = monthly_spend * weight * 12
        wanted = SCENARIO_RULES.get(scenario, {scenario})
        if scenario == selected_category_scenario:
            wanted = {*wanted, "selected_lifestyle"}
        candidates = [rule for rule in product.reward_rules
                      if rule.spend_category in wanted
                      and _rule_qualifies(rule, scenario, facts, selected_category_scenario)
                      and _rule_active(rule, now)]
        if not candidates:
            # No specialised rule: the general base rate still applies.
            if base_rule is not None:
                annual_cash = _add_reward_value(annual_cash, units, base_rule, annual_spend)
            continue
        totals = [rule for rule in candidates if rule.stacking == "total"]
        extras = [rule for rule in candidates if rule.stacking == "extra"]
        # Total layer: best specialised total rule, otherwise the base rate.
        total_rule = max(totals, key=_reward_value) if totals else base_rule
        if total_rule is None:
            continue
        in_cap = _annual_value_within_cap(annual_spend, _annual_cap(total_rule))
        annual_cash = _add_reward_value(annual_cash, units, total_rule, in_cap)
        # Spending above the total-rule cap falls back to the base rate.
        overflow = annual_spend - in_cap
        if overflow > 0 and base_rule is not None and base_rule is not total_rule:
            annual_cash = _add_reward_value(annual_cash, units, base_rule, overflow)
        # Extra layers stack on top, each capped independently.
        for extra in extras:
            annual_cash = _add_reward_value(
                annual_cash, units, extra,
                _annual_value_within_cap(annual_spend, _annual_cap(extra)))
            if extra.valid_until is not None:
                promo_rules.append(extra.description or extra.rule_id)
        if totals or extras:
            matched.append(scenario)
    coverage = sum(
        (weight for scenario, weight in zip(scenarios, weights) if scenario in matched),
        Decimal("0"),
    )
    return annual_cash, units, matched, coverage, promo_rules


def _rule_active(rule, now) -> bool:
    """Promotional rules stop contributing once their window has passed."""
    return now is None or rule.valid_until is None or rule.valid_until >= now


def _annual_cap(rule) -> Decimal | None:
    """Annualised eligible-spend cap of a rule (None when uncapped)."""
    if rule is None or rule.spend_cap is None:
        return None
    if rule.cap_period == "month":
        return rule.spend_cap * 12
    if rule.cap_period == "quarter":
        return rule.spend_cap * 4
    return rule.spend_cap  # year or unspecified period


def _add_reward_value(annual_cash, units, rule, annual_spend):
    if rule is None or annual_spend <= 0:
        return annual_cash
    cash_rate = rule.cash_equivalent_rate
    if cash_rate is None and rule.reward_type == "cashback":
        cash_rate = rule.reward_rate
    if cash_rate is not None:
        annual_cash += annual_spend * cash_rate
    if rule.points_per_currency_unit is not None:
        units[rule.reward_type] = units.get(rule.reward_type, Decimal("0")) + \
            annual_spend * rule.points_per_currency_unit
    return annual_cash


def _perpetually_waived(annual) -> bool:
    waiver = (annual.waiver_condition or "").casefold()
    return annual.amount == 0 or any(token in waiver for token in (
        "annual fee waived", "permanent waiver", "perpetual", "waived while an eligible",
        "永久免", "豁免年费"))


def _estimated_annual_fee(annual, eligibility: EligibilityAssessment,
                          annual_eligible_spend: Decimal | None = None) -> Decimal:
    """Fee over a two-year horizon, net of introductory and spend-threshold waivers."""
    if annual is None or annual.amount == 0:
        return Decimal("0")
    waiver = (annual.waiver_condition or "").casefold()
    if any(token in waiver for token in (
            "perpetual", "permanent", "annual fee waived", "永久免", "豁免年费")):
        return Decimal("0")
    if "waived while an eligible" in waiver and eligibility.status in {
            "eligible", "needs_bank_assessment"}:
        return Decimal("0")
    # Spend-threshold waiver (e.g. EveryMile: HKD80,000 annual eligible spend
    # waives every subsequent year, so the two-year horizon stays free).
    if (annual.waiver_min_annual_spend_hkd is not None
            and annual_eligible_spend is not None
            and annual_eligible_spend >= annual.waiver_min_annual_spend_hkd):
        return Decimal("0")
    # Compare the first two years. A two-year introductory waiver costs zero
    # during this horizon; a one-year waiver amortises year two over the period.
    if any(token in waiver for token in (
            "first two", "first 2", "2-year", "two-year", "首两", "首 2")):
        return Decimal("0")
    if any(token in waiver for token in ("first year", "first-year", "首年")):
        return (annual.amount or Decimal("0")) / 2
    return annual.amount or Decimal("0")


def _ongoing_annual_fee(annual, annual_eligible_spend: Decimal | None = None) -> Decimal:
    """Annual fee from year three onwards, once introductory waivers expire."""
    if annual is None or annual.amount == 0:
        return Decimal("0")
    if _perpetually_waived(annual):
        return Decimal("0")
    if (annual.waiver_min_annual_spend_hkd is not None
            and annual_eligible_spend is not None
            and annual_eligible_spend >= annual.waiver_min_annual_spend_hkd):
        return Decimal("0")
    return annual.amount or Decimal("0")


def _benefit_points(product: CreditCardProduct, scenarios: list[str]) -> Decimal:
    """Score structured benefits only when they address a selected scenario."""
    kinds = {benefit.benefit_type.casefold() for benefit in product.benefits}
    text = " ".join(
        f"{benefit.benefit_name} {benefit.description or ''}" for benefit in product.benefits
    ).casefold()
    score = Decimal("0")
    if "mainland_china" in scenarios:
        if "dual_currency" in kinds:
            score += 4
        if any(token in text for token in ("mainland", "china", "macau", "内地", "澳门")):
            score += 3
    if "travel" in scenarios and kinds & {"travel_insurance", "lounge_access", "airport_lounge"}:
        score += 3
    if "online" in scenarios and kinds & {"digital_wallet", "mobile_wallet"}:
        score += 1
    return min(score, Decimal("8"))


def _estimated_annual_fx_cost(product: CreditCardProduct, scenarios: list[str],
                              monthly_spend: Decimal) -> Decimal:
    fx = _fee(product, "foreign_transaction_fee")
    if fx is None:
        return Decimal("0")
    kinds = {benefit.benefit_type.casefold() for benefit in product.benefits}
    weights = _scenario_weights(scenarios)
    total = Decimal("0")
    for scenario, weight in zip(scenarios, weights):
        rate = fx.rate or Decimal("0")
        if scenario == "mainland_china":
            # Dual-currency cards settle RMB (and MOP for Pulse) through the
            # RMB account, where the foreign-transaction fee is waived.
            if "dual_currency" in kinds:
                rate = Decimal("0")
        elif scenario in {"overseas", "travel"}:
            # Foreign currencies other than RMB/MOP pay the card's fx rate,
            # including on dual-currency cards (1% for non-RMB/MOP currencies).
            pass
        else:
            continue
        total += monthly_spend * weight * 12 * rate
    return total


def score_product(product: CreditCardProduct, eligibility: EligibilityAssessment,
                  scenarios: list[str], preference: str,
                  monthly_spend: Decimal = Decimal("5000"),
                  facts: dict | None = None, now=None) -> CardRecommendation:
    facts = facts or {}
    annual_reward, reward_units, matched, coverage, promo_rules = _annual_reward_metrics(
        product, scenarios, monthly_spend, facts, now)

    annual = _fee(product, "annual_fee")
    annual_eligible_spend = monthly_spend * 12
    annual_fee = _estimated_annual_fee(annual, eligibility, annual_eligible_spend)
    ongoing_fee = _ongoing_annual_fee(annual, annual_eligible_spend)

    fx = _fee(product, "foreign_transaction_fee")
    annual_fx_cost = _estimated_annual_fx_cost(product, scenarios, monthly_spend)
    benefit_score = _benefit_points(product, scenarios)
    source_flags = [bool(item.source_url and item.last_verified_at)
                    for item in [*product.reward_rules, *product.fees, *product.benefits]]
    data_confidence = (Decimal(sum(source_flags)) / Decimal(len(source_flags))
                       if source_flags else Decimal("0"))
    net_value = annual_reward - annual_fee - annual_fx_cost
    breakdown = {
        "annual_reward_hkd": annual_reward.quantize(Decimal("0.01")),
        "annual_fee_hkd": (-annual_fee).quantize(Decimal("0.01")),
        "annual_fx_cost_hkd": (-annual_fx_cost).quantize(Decimal("0.01")),
    }
    # Eligibility reasons stay on eligibility for ranking/debug; do not dump the
    # raw 满足/不满足 lines into user-facing limitations.
    limitations = []
    if not product.official_url:
        limitations.append("官网链接暂缺，申请前请到发卡机构网站核对。")
    purchase = _borrowing_term(product, "purchase_apr")
    cash = _borrowing_term(product, "cash_advance_apr")
    interest_free = _borrowing_term(product, "interest_free_period")
    borrowing_sources = list(dict.fromkeys(
        term.source_url for term in product.borrowing_terms if term.source_url))
    return CardRecommendation(
        card_id=product.card_id, product_name=product.product_name, issuing_bank=product.issuing_bank,
        card_network=product.card_network, role="alternative", eligibility=eligibility,
        score=net_value.quantize(Decimal("0.01")), score_breakdown=breakdown,
        estimated_monthly_spend_hkd=monthly_spend,
        estimated_annual_reward_hkd=annual_reward.quantize(Decimal("0.01")),
        estimated_annual_fee_hkd=annual_fee.quantize(Decimal("0.01")),
        ongoing_annual_fee_hkd=ongoing_fee.quantize(Decimal("0.01")),
        estimated_annual_fx_cost_hkd=annual_fx_cost.quantize(Decimal("0.01")),
        estimated_annual_net_value_hkd=net_value.quantize(Decimal("0.01")),
        reward_units={key: value.quantize(Decimal("0.01")) for key, value in reward_units.items()},
        coverage_weight=coverage.quantize(Decimal("0.01")),
        benefit_fit=benefit_score.quantize(Decimal("0.01")),
        data_confidence=data_confidence.quantize(Decimal("0.01")),
        matched_scenarios=matched, promo_rules=promo_rules,
        annual_fee=annual.amount if annual else None,
        annual_fee_currency=annual.currency if annual else None,
        annual_fee_waiver=annual.waiver_condition if annual else None,
        foreign_transaction_fee_rate=fx.rate if fx else None,
        main_benefits=[benefit.benefit_name for benefit in product.benefits],
        limitations=limitations, official_url=product.official_url,
        purchase_apr=purchase.apr if purchase else None,
        cash_advance_apr=cash.apr if cash else None,
        max_interest_free_days=(interest_free.max_interest_free_days
                                if interest_free else None),
        borrowing_sources=borrowing_sources)


def recommend(products: list[CreditCardProduct], facts: dict, scenarios: list[str],
              preference: str = "no_preference", now=None) -> CreditCardRecommendationSet:
    scored, excluded = [], {}
    by_id = {product.card_id: product for product in products}
    for product in products:
        eligibility = assess_eligibility(product, facts)
        if eligibility.status == "ineligible":
            excluded[product.card_id] = eligibility.reasons
            continue
        monthly_spend = MONTHLY_SPEND_ESTIMATES.get(
            facts.get("monthly_card_spend_band_hkd"), Decimal("5000"))
        scored.append(score_product(
            product, eligibility, scenarios, preference, monthly_spend, facts=facts, now=now))
    if not scored:
        return CreditCardRecommendationSet(excluded=excluded)

    eligibility_rank = {"eligible": 3, "needs_bank_assessment": 2, "unknown": 1}

    def compare(left, right):
        left_eligibility = eligibility_rank.get(left.eligibility.status, 0)
        right_eligibility = eligibility_rank.get(right.eligibility.status, 0)
        if left_eligibility != right_eligibility:
            return -1 if left_eligibility > right_eligibility else 1
        if preference in {"points", "miles"}:
            left_units = left.reward_units.get(preference, Decimal("0"))
            right_units = right.reward_units.get(preference, Decimal("0"))
            if left_units != right_units:
                return -1 if left_units > right_units else 1
        tolerance = max(abs(left.score), abs(right.score), Decimal("100")) * Decimal("0.05")
        if abs(left.score - right.score) > tolerance:
            return -1 if left.score > right.score else 1
        # Within the value tolerance, prefer the card that stays cheap once
        # introductory waivers expire (year three onwards).
        if left.ongoing_annual_fee_hkd != right.ongoing_annual_fee_hkd:
            return -1 if left.ongoing_annual_fee_hkd < right.ongoing_annual_fee_hkd else 1
        if left.benefit_fit != right.benefit_fit:
            return -1 if left.benefit_fit > right.benefit_fit else 1
        if left.coverage_weight != right.coverage_weight:
            return -1 if left.coverage_weight > right.coverage_weight else 1
        if left.data_confidence != right.data_confidence:
            return -1 if left.data_confidence > right.data_confidence else 1
        if left.score != right.score:
            return -1 if left.score > right.score else 1
        return (left.card_id > right.card_id) - (left.card_id < right.card_id)

    scored.sort(key=cmp_to_key(compare))
    ranked_candidates = [item.model_copy(update={"role": "alternative"}) for item in scored]
    primary = scored[0].model_copy(update={"role": "primary"})
    selected = {primary.card_id}
    gaps = set(scenarios) - set(primary.matched_scenarios)
    supplements = []
    for item in scored[1:]:
        if item.eligibility.status not in {"eligible", "needs_bank_assessment"}:
            continue
        new_coverage = gaps & set(item.matched_scenarios)
        if not new_coverage:
            continue
        supplements.append(item.model_copy(update={"role": "supplement"}))
        selected.add(item.card_id)
        gaps -= new_coverage
        if not gaps or len(supplements) == 2:
            break

    alternatives = []
    for item in scored[1:]:
        if item.card_id in selected:
            continue
        if item.eligibility.status not in {"eligible", "needs_bank_assessment"}:
            continue
        annual = _fee(by_id[item.card_id], "annual_fee")
        if annual and annual.amount == 0:
            alternatives.append(item.model_copy(update={"role": "alternative"}))
        if len(alternatives) == 3:
            break
    return CreditCardRecommendationSet(primary=primary, supplements=supplements,
                                       alternatives=alternatives, ranked_candidates=ranked_candidates,
                                       excluded=excluded)
