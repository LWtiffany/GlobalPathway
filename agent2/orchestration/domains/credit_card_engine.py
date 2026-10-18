"""Pure eligibility and ranking rules for credit-card recommendations."""

from decimal import Decimal

from .credit_card_models import (CardRecommendation, CreditCardProduct,
                                 CreditCardRecommendationSet, EligibilityAssessment)

SCENARIO_RULES = {
    "general": {"general"},
    "online": {"online", "selected_retail", "selected_lifestyle"},
    "dining": {"dining_and_lifestyle", "travel_and_dining", "selected_lifestyle"},
    "travel": {"travel_and_transport", "travel_and_dining", "overseas"},
    "overseas": {"overseas", "travel_and_transport", "travel_and_dining"},
    "mainland_china": {"mainland_china", "mainland_china_and_macau"},
    "tuition": {"tuition"},
    "retail": {"selected_retail", "selected_lifestyle"},
}


def assess_eligibility(product: CreditCardProduct, facts: dict) -> EligibilityAssessment:
    reasons, missing = [], []
    ineligible = False
    for rule in product.eligibility_rules:
        if not rule.required:
            continue
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
        elif rule.rule_type == "student_status":
            value, field = facts.get("full_time_student"), "full_time_student"
            passed = value is True
        elif rule.rule_type == "banking_relationship":
            value, field = facts.get("premier_relationship"), "premier_relationship"
            passed = value is True
        else:
            field = {"residency": "residency_status", "credit_assessment": "credit_history_band"}.get(
                rule.rule_type, f"card_rule_{rule.rule_type}")
            value, passed = facts.get(field), False
        if value is None:
            missing.append(field)
            reasons.append(f"缺少门槛信息：{rule.description or rule.rule_type}")
        elif not passed:
            ineligible = True
            reasons.append(f"不满足：{rule.description or rule.rule_type}")
    status = "ineligible" if ineligible else "unknown" if missing else "eligible"
    return EligibilityAssessment(status=status, reasons=reasons,
                                 missing_fields=list(dict.fromkeys(missing)))


def _fee(product, fee_type):
    return next((fee for fee in product.fees if fee.fee_type == fee_type), None)


def _best_reward(product, scenario):
    wanted = SCENARIO_RULES.get(scenario, {scenario})
    exact = [rule for rule in product.reward_rules if rule.spend_category in wanted]
    rules = exact or [rule for rule in product.reward_rules if rule.spend_category == "general"]
    if not rules:
        return None, False

    def value(rule):
        return rule.reward_rate or rule.points_per_currency_unit or Decimal("0")

    return max(rules, key=value), bool(exact)


def _reward_points(rule, preference):
    if rule is None:
        return Decimal("0")
    if rule.reward_type == "cashback":
        value = min((rule.reward_rate or Decimal("0")) / Decimal("0.05"), Decimal("1")) * 40
    elif rule.reward_type == "points":
        value = min((rule.points_per_currency_unit or Decimal("0")) / Decimal("6"), Decimal("1")) * 35
    else:
        value = min((rule.points_per_currency_unit or Decimal("0")) / Decimal("0.5"), Decimal("1")) * 35
    if preference == rule.reward_type:
        value += 5
    return value


def score_product(product: CreditCardProduct, eligibility: EligibilityAssessment,
                  scenarios: list[str], preference: str) -> CardRecommendation:
    rewards, matched = [], []
    for scenario in scenarios:
        rule, specialized = _best_reward(product, scenario)
        rewards.append(_reward_points(rule, preference))
        if specialized:
            matched.append(scenario)
    reward_score = sum(rewards, Decimal("0")) / max(len(rewards), 1)
    coverage_score = Decimal(len(matched)) / max(len(scenarios), 1) * 25

    annual = _fee(product, "annual_fee")
    if annual and annual.amount == 0:
        cost_score = Decimal("10")
    elif annual and annual.waiver_condition:
        cost_score = Decimal("3") - min((annual.amount or 0) / 100, Decimal("20"))
    elif annual:
        cost_score = -min((annual.amount or 0) / 100, Decimal("20"))
    else:
        cost_score = Decimal("0")

    fx = _fee(product, "foreign_transaction_fee")
    fx_score = Decimal("0")
    if {"overseas", "mainland_china", "travel"} & set(scenarios) and fx:
        fx_score = Decimal("10") if fx.rate == 0 else -min((fx.rate or 0) * 500, Decimal("10"))
    eligibility_score = Decimal("10") if eligibility.status == "eligible" else Decimal("0")
    data_score = Decimal("5") if product.data_status == "verified" else Decimal("0")
    breakdown = {"scenario_value": reward_score.quantize(Decimal("0.01")),
                 "coverage": coverage_score.quantize(Decimal("0.01")),
                 "fees": cost_score.quantize(Decimal("0.01")),
                 "foreign_spend": fx_score.quantize(Decimal("0.01")),
                 "eligibility": eligibility_score, "data_quality": data_score}
    score = sum(breakdown.values(), Decimal("0"))
    limitations = list(eligibility.reasons)
    if product.data_status != "verified":
        limitations.append(f"产品数据状态为 {product.data_status}，需用官方页面复核。")
    if not product.official_url:
        limitations.append("RDS 没有官方产品链接。")
    return CardRecommendation(
        card_id=product.card_id, product_name=product.product_name, issuing_bank=product.issuing_bank,
        card_network=product.card_network, role="alternative", eligibility=eligibility,
        score=score.quantize(Decimal("0.01")), score_breakdown=breakdown,
        matched_scenarios=matched, annual_fee=annual.amount if annual else None,
        annual_fee_currency=annual.currency if annual else None,
        annual_fee_waiver=annual.waiver_condition if annual else None,
        foreign_transaction_fee_rate=fx.rate if fx else None,
        main_benefits=[benefit.benefit_name for benefit in product.benefits],
        limitations=limitations, official_url=product.official_url, data_status=product.data_status)


def recommend(products: list[CreditCardProduct], facts: dict, scenarios: list[str],
              preference: str = "no_preference") -> CreditCardRecommendationSet:
    scored, excluded = [], {}
    by_id = {product.card_id: product for product in products}
    for product in products:
        eligibility = assess_eligibility(product, facts)
        if eligibility.status == "ineligible":
            excluded[product.card_id] = eligibility.reasons
            continue
        scored.append(score_product(product, eligibility, scenarios, preference))
    if not scored:
        return CreditCardRecommendationSet(excluded=excluded)

    scored.sort(key=lambda item: (item.eligibility.status == "eligible", item.score), reverse=True)
    primary = scored[0].model_copy(update={"role": "primary"})
    selected = {primary.card_id}
    gaps = set(scenarios) - set(primary.matched_scenarios)
    supplements = []
    for item in scored[1:]:
        if item.eligibility.status != "eligible":
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
        if item.eligibility.status != "eligible":
            continue
        annual = _fee(by_id[item.card_id], "annual_fee")
        if annual and annual.amount == 0:
            alternatives.append(item.model_copy(update={"role": "alternative"}))
        if len(alternatives) == 3:
            break
    return CreditCardRecommendationSet(primary=primary, supplements=supplements,
                                       alternatives=alternatives, excluded=excluded)
