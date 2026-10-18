"""Acceptance tests for layered reward stacking, promo expiry, fee waivers and
the honest-disclosure behaviours introduced after the 2026-10 model audit."""

import asyncio
import unittest
from datetime import datetime, timezone
from decimal import Decimal

from agent2.orchestration.contracts import DomainTask
from agent2.orchestration.domains.credit_card import CreditCardWorkflow
from agent2.orchestration.domains.credit_card_engine import (
    assess_eligibility, recommend, score_product,
)
from agent2.orchestration.domains.credit_card_models import (
    CardBenefit, CardFee, EligibilityRule, RewardRule,
)

from tests.test_credit_card_workflow import (FakeRepository, NOW, TASK, product, request)

PROMO_END = datetime(2026, 12, 31, 15, 59, 59, tzinfo=timezone.utc)
AFTER_PROMO = datetime(2027, 1, 5, tzinfo=timezone.utc)
DURING_PROMO = datetime(2026, 10, 18, tzinfo=timezone.utc)


def pulse_style_card():
    """Official Pulse structure: base + QR extra (capped, promotional) + Your
    Choice extra (capped per programme year)."""
    card = product("pulse_card", "mainland_china_and_macau", "0.020")
    sourced = {"source_url": "https://example.com/pulse_card", "last_verified_at": NOW}
    return card.model_copy(update={"reward_rules": [
        RewardRule(rule_id="pulse-base", spend_category="general", reward_type="cashback",
                   reward_rate=Decimal("0.004"), cash_equivalent_rate=Decimal("0.004"),
                   **sourced),
        RewardRule(rule_id="pulse-qr", spend_category="mainland_china_and_macau",
                   reward_type="cashback", reward_rate=Decimal("0.020"),
                   cash_equivalent_rate=Decimal("0.020"),
                   spend_cap=Decimal("80000"), cap_currency="HKD", cap_period="year",
                   stacking="extra", valid_until=PROMO_END,
                   qualification_type="payment_method", qualification_value="mobile_or_qr",
                   description=("Extra 2.0% RewardCash for QR/mobile payments in Mainland "
                                "China and Macau (promotional, per HKID, until 31 December 2026)"),
                   **sourced),
        RewardRule(rule_id="pulse-yc", spend_category="selected_lifestyle",
                   reward_type="cashback", reward_rate=Decimal("0.020"),
                   cash_equivalent_rate=Decimal("0.020"),
                   spend_cap=Decimal("100000"), cap_currency="HKD", cap_period="year",
                   stacking="extra", qualification_type="selected_category",
                   **sourced),
    ]})


def your_choice_card(card_id="yc_card"):
    card = product(card_id, "selected_lifestyle", "0.020")
    sourced = {"source_url": f"https://example.com/{card_id}", "last_verified_at": NOW}
    return card.model_copy(update={"reward_rules": [
        RewardRule(rule_id=f"{card_id}-base", spend_category="general", reward_type="cashback",
                   reward_rate=Decimal("0.004"), cash_equivalent_rate=Decimal("0.004"),
                   **sourced),
        RewardRule(rule_id=f"{card_id}-yc", spend_category="selected_lifestyle",
                   reward_type="cashback", reward_rate=Decimal("0.020"),
                   cash_equivalent_rate=Decimal("0.020"),
                   spend_cap=Decimal("100000"), cap_currency="HKD", cap_period="year",
                   stacking="extra", qualification_type="selected_category",
                   **sourced),
    ]})


def everymile_style_card():
    card = product("miles_card", "travel_and_transport", "0.025")
    return card.model_copy(update={
        "reward_rules": [
            RewardRule(rule_id="miles-base", spend_category="general", reward_type="miles",
                       points_per_currency_unit=Decimal("0.2"),
                       cash_equivalent_rate=Decimal("0.010"),
                       source_url="https://example.com/miles_card", last_verified_at=NOW),
            RewardRule(rule_id="miles-travel", spend_category="travel_and_transport",
                       reward_type="miles", points_per_currency_unit=Decimal("0.5"),
                       cash_equivalent_rate=Decimal("0.025"),
                       source_url="https://example.com/miles_card", last_verified_at=NOW),
        ],
        "fees": [CardFee(
            fee_id="miles-annual", fee_type="annual_fee", amount=Decimal("2000"),
            currency="HKD",
            waiver_condition=("First year waived; subsequent years waived with "
                              "HKD80,000 annual eligible spend"),
            waiver_min_annual_spend_hkd=Decimal("80000"),
            source_url="https://example.com/miles_card", last_verified_at=NOW)],
    })


def dual_currency_card(card_id="dual_card"):
    card = product(card_id, "selected_lifestyle", "0.020", network="UnionPay")
    sourced = {"source_url": f"https://example.com/{card_id}", "last_verified_at": NOW}
    return card.model_copy(update={
        "benefits": [CardBenefit(benefit_id=f"{card_id}-dual", benefit_type="dual_currency",
                                 benefit_name="HKD and RMB dual currency", **sourced)],
        "fees": [
            CardFee(fee_id=f"{card_id}-annual", fee_type="annual_fee", amount=Decimal("300"),
                    currency="HKD", waiver_condition="First two years annual fee waiver",
                    **sourced),
            CardFee(fee_id=f"{card_id}-fx", fee_type="foreign_transaction_fee",
                    rate=Decimal("0.01"),
                    scope_text="1% for currencies other than RMB/HKD/MOP; RMB and MOP are waived",
                    **sourced),
        ],
    })


def student_product():
    card = product("student_card", "general", "0.004", income=0)
    return card.model_copy(update={"eligibility_rules": [
        EligibilityRule(rule_id="student-age", rule_type="minimum_age", operator="gte",
                        value_numeric=Decimal("18")),
        EligibilityRule(rule_id="student-status", rule_type="student_status", operator="eq",
                        value_text="Full-time student in Hong Kong"),
        EligibilityRule(rule_id="student-institution", rule_type="institution_status",
                        operator="eq", value_text="HSBC listed institution"),
    ]})


class LayeredRewardStackingTests(unittest.TestCase):
    def test_pulse_layers_stack_with_independent_caps(self):
        card = pulse_style_card()
        facts = {"applicant_age": 25, "annual_income_hkd": 150000,
                 "mainland_payment_method": "mobile_or_qr"}
        eligibility = assess_eligibility(card, facts)

        scored = score_product(card, eligibility, ["mainland_china"], "no_preference",
                               Decimal("30000"), facts=facts, now=DURING_PROMO)

        # base 0.4% x 360,000 + QR extra 2% x 80,000 + Your Choice 2% x 100,000
        self.assertEqual(Decimal("5040.00"), scored.estimated_annual_reward_hkd)
        self.assertEqual(["mainland_china"], scored.matched_scenarios)
        self.assertEqual(1, len(scored.promo_rules))  # only the QR layer is promotional

    def test_your_choice_annual_cap_limits_accelerated_layer(self):
        card = your_choice_card()
        eligibility = assess_eligibility(
            card, {"applicant_age": 25, "annual_income_hkd": 150000})

        scored = score_product(card, eligibility, ["dining"], "no_preference",
                               Decimal("30000"))

        # base 0.4% x 360,000 + Your Choice 2% on the first 100,000 only.
        self.assertEqual(Decimal("3440.00"), scored.estimated_annual_reward_hkd)

    def test_expired_promotional_layer_stops_contributing(self):
        card = pulse_style_card()
        facts = {"applicant_age": 25, "annual_income_hkd": 150000,
                 "mainland_payment_method": "mobile_or_qr"}
        eligibility = assess_eligibility(card, facts)

        after = score_product(card, eligibility, ["mainland_china"], "no_preference",
                              Decimal("30000"), facts=facts, now=AFTER_PROMO)

        # QR promo gone: base 0.4% x 360,000 + Your Choice 2% x 100,000.
        self.assertEqual(Decimal("3440.00"), after.estimated_annual_reward_hkd)
        self.assertEqual([], after.promo_rules)

    def test_promo_expiry_flips_recommendation_ranking(self):
        """A promo that wins today must not silently win after it expires."""
        card = pulse_style_card()
        plain = product("plain_card", "mainland_china", "0.012")
        facts = {"applicant_age": 25, "annual_income_hkd": 150000,
                 "mainland_payment_method": "mobile_or_qr",
                 "monthly_card_spend_band_hkd": "20k_plus"}

        during = recommend([card, plain], facts, ["mainland_china"],
                           "no_preference", now=DURING_PROMO)
        after = recommend([card, plain], facts, ["mainland_china"],
                          "no_preference", now=AFTER_PROMO)

        # During the promo: pulse 5,040 beats plain 0.012 x 360,000 = 4,320.
        self.assertEqual("pulse_card", during.primary.card_id)
        # After the promo: pulse drops to 3,440 and plain takes over.
        self.assertEqual("plain_card", after.primary.card_id)


class AnnualFeeWaiverTests(unittest.TestCase):
    def test_spend_threshold_waives_everymile_style_fee(self):
        card = everymile_style_card()
        eligibility = assess_eligibility(
            card, {"applicant_age": 25, "annual_income_hkd": 300000})

        # HKD10,000/month -> 120,000/year >= 80,000 -> fully waived.
        heavy = score_product(card, eligibility, ["travel"], "no_preference", Decimal("10000"))
        self.assertEqual(Decimal("0.00"), heavy.estimated_annual_fee_hkd)
        self.assertEqual(Decimal("0.00"), heavy.ongoing_annual_fee_hkd)

        # HKD5,000/month -> 60,000/year < 80,000 -> year two costs 2,000 (amortised 1,000).
        light = score_product(card, eligibility, ["travel"], "no_preference", Decimal("5000"))
        self.assertEqual(Decimal("1000.00"), light.estimated_annual_fee_hkd)
        self.assertEqual(Decimal("2000.00"), light.ongoing_annual_fee_hkd)

    def test_ongoing_fee_breaks_near_value_ties(self):
        cheap = product("cheap_forever", "online", "0.04")  # perpetual fee waiver
        intro = product("intro_card", "online", "0.04")
        intro = intro.model_copy(update={"fees": [CardFee(
            fee_id="intro-annual", fee_type="annual_fee", amount=Decimal("1800"),
            currency="HKD", waiver_condition="First two years annual fee waiver")]})

        result = recommend([intro, cheap], {"applicant_age": 25, "annual_income_hkd": 200000},
                           ["online"])

        ranked = {card.card_id: card for card in result.ranked_candidates}
        self.assertEqual(Decimal("1800.00"), ranked["intro_card"].ongoing_annual_fee_hkd)
        self.assertEqual(Decimal("0.00"), ranked["cheap_forever"].ongoing_annual_fee_hkd)
        # Both are free in years 1-2, so the ongoing fee must decide.
        self.assertEqual("cheap_forever", result.primary.card_id)


class DualCurrencyFxTests(unittest.TestCase):
    def test_dual_currency_pays_fx_on_foreign_travel(self):
        card = dual_currency_card()
        eligibility = assess_eligibility(
            card, {"applicant_age": 25, "annual_income_hkd": 150000})

        scored = score_product(card, eligibility, ["travel"], "no_preference",
                               Decimal("7500"))

        # Non-RMB/MOP currencies pay 1%: 7,500 x 12 x 1% = 900.
        self.assertEqual(Decimal("900.00"), scored.estimated_annual_fx_cost_hkd)

    def test_dual_currency_keeps_rmb_mainland_fx_waiver(self):
        card = dual_currency_card()
        eligibility = assess_eligibility(
            card, {"applicant_age": 25, "annual_income_hkd": 150000})

        scored = score_product(card, eligibility, ["mainland_china"], "no_preference",
                               Decimal("7500"))

        # RMB settled through the RMB account stays free.
        self.assertEqual(Decimal("0.00"), scored.estimated_annual_fx_cost_hkd)


class MilesPresentationTests(unittest.TestCase):
    def test_miles_primary_shows_units_and_positive_value(self):
        card = everymile_style_card()
        eligibility = assess_eligibility(
            card, {"applicant_age": 25, "annual_income_hkd": 300000})
        card = card.model_copy(update={"fees": list(card.fees) + [CardFee(
            fee_id="miles-fx", fee_type="foreign_transaction_fee", rate=Decimal("0.0195"))]})

        scored = score_product(card, eligibility, ["travel"], "no_preference",
                               Decimal("15000"))

        # 2.5% equivalent on 180,000 minus 1.95% fx = +990, with 90,000 miles.
        self.assertEqual(Decimal("4500.00"), scored.estimated_annual_reward_hkd)
        self.assertEqual(Decimal("990.00"), scored.estimated_annual_net_value_hkd)
        self.assertEqual(Decimal("90000.00"), scored.reward_units["miles"])

    def test_miles_answer_text_includes_mile_units(self):
        card = everymile_style_card()
        card = card.model_copy(update={"fees": list(card.fees) + [CardFee(
            fee_id="miles-fx", fee_type="foreign_transaction_fee", rate=Decimal("0.0195"),
            source_url="https://example.com/miles_card", last_verified_at=NOW)]})
        workflow = CreditCardWorkflow(FakeRepository([card]), clock=lambda: NOW)

        result = asyncio.run(workflow.run(
            TASK, request(annual_income_hkd=300000,
                          monthly_card_spend_band_hkd="10k_20k",
                          spending_scenarios=["travel"], reward_preference="miles")))

        self.assertEqual("answered", result.status)
        self.assertIn("飞行里程", result.answer.steps[0])

    def test_intro_waiver_cards_disclose_post_waiver_fee(self):
        card = dual_currency_card()
        workflow = CreditCardWorkflow(FakeRepository([card]), clock=lambda: NOW)

        result = asyncio.run(workflow.run(
            TASK, request(annual_income_hkd=150000, spending_scenarios=["travel"])))

        self.assertEqual("answered", result.status)
        # Free for two years, but the answer must disclose the year-three fee.
        self.assertIn("豁免期后年费 HK$300/年", result.answer.steps[0])


class StudentGateTests(unittest.TestCase):
    def test_low_income_applicant_is_asked_about_student_status(self):
        workflow = CreditCardWorkflow(
            FakeRepository([product("normal_card", "online", "0.04", income=120000),
                            student_product()]),
            clock=lambda: NOW)

        result = asyncio.run(workflow.run(
            TASK, request(annual_income_hkd=50000, annual_income_band_hkd=None)))

        self.assertEqual("needs_input", result.status)
        self.assertEqual(["full_time_student"],
                         [question.field for question in result.follow_up_questions])

    def test_student_follow_up_asks_institution_then_recommends(self):
        workflow = CreditCardWorkflow(
            FakeRepository([product("normal_card", "online", "0.04", income=120000),
                            student_product()]),
            clock=lambda: NOW)

        second = asyncio.run(workflow.run(
            TASK, request(annual_income_hkd=50000, full_time_student=True)))

        self.assertEqual(["student_institution_eligible"],
                         [question.field for question in second.follow_up_questions])

        third = asyncio.run(workflow.run(
            TASK, request(annual_income_hkd=50000, full_time_student=True,
                          student_institution_eligible=True)))

        self.assertEqual("answered", third.status)
        self.assertEqual("student_card",
                         third.payload["recommendations"]["primary"]["card_id"])

    def test_mainstream_income_never_sees_student_questions(self):
        workflow = CreditCardWorkflow(
            FakeRepository([product("normal_card", "online", "0.04", income=120000),
                            student_product()]),
            clock=lambda: NOW)

        result = asyncio.run(workflow.run(
            TASK, request(annual_income_hkd=200000, repayment_mode=None,
                          spending_scenarios=None)))

        self.assertEqual("needs_input", result.status)
        self.assertEqual(["repayment_mode", "spending_scenarios"],
                         [question.field for question in result.follow_up_questions])


class DisclosureTests(unittest.TestCase):
    def test_answer_discloses_scope_welcome_and_promo(self):
        card = pulse_style_card()
        card = card.model_copy(update={
            "fees": list(card.fees) + [CardFee(
                fee_id="pulse-fx", fee_type="foreign_transaction_fee", rate=Decimal("0.01"),
                source_url="https://example.com/pulse_card", last_verified_at=NOW)]})
        workflow = CreditCardWorkflow(FakeRepository([card]), clock=lambda: NOW)

        result = asyncio.run(workflow.run(TASK, request(
            annual_income_hkd=150000, spending_scenarios=["mainland_china"],
            mainland_payment_method="mobile_or_qr")))

        self.assertEqual("answered", result.status)
        joined = " ".join(result.answer.limitations)
        self.assertIn("仅覆盖 HSBC", joined)
        self.assertIn("迎新礼遇", joined)
        self.assertIn("限时推广", joined)
        self.assertIn("QR/mobile payments", joined)

    def test_multi_card_combo_discloses_shared_your_choice_cap(self):
        primary = your_choice_card("yc_primary")
        # A strong dining total rule keeps this card the primary overall.
        primary = primary.model_copy(update={"reward_rules": [
            primary.reward_rules[0],
            primary.reward_rules[1],  # Your Choice layer on the selected category
            RewardRule(rule_id="yc-primary-dining", spend_category="dining_and_lifestyle",
                       reward_type="cashback", reward_rate=Decimal("0.05"),
                       cash_equivalent_rate=Decimal("0.05"),
                       source_url="https://example.com/yc_primary", last_verified_at=NOW),
        ]})
        supplement = your_choice_card("yc_supp")
        # Weaker overall, but the only card covering the travel gap.
        supplement = supplement.model_copy(update={"reward_rules": [
            supplement.reward_rules[0],
            supplement.reward_rules[1],  # Your Choice layer on the selected category
            RewardRule(rule_id="yc-supp-travel", spend_category="travel_and_transport",
                       reward_type="cashback", reward_rate=Decimal("0.02"),
                       cash_equivalent_rate=Decimal("0.02"),
                       source_url="https://example.com/yc_supp", last_verified_at=NOW),
        ]})
        workflow = CreditCardWorkflow(
            FakeRepository([primary, supplement]), clock=lambda: NOW)

        result = asyncio.run(workflow.run(TASK, request(
            annual_income_hkd=200000, spending_scenarios=["dining", "travel"])))

        self.assertEqual("yc_primary",
                         result.payload["recommendations"]["primary"]["card_id"])
        self.assertEqual(["yc_supp"], [item["card_id"] for item in
                                       result.payload["recommendations"]["supplements"]])
        self.assertIn("身份证合计", " ".join(result.answer.limitations))

    def test_single_card_answer_has_no_combo_disclaimer(self):
        card = your_choice_card()
        workflow = CreditCardWorkflow(FakeRepository([card]), clock=lambda: NOW)

        result = asyncio.run(workflow.run(TASK, request(spending_scenarios=["dining"])))

        self.assertNotIn("身份证合计", " ".join(result.answer.limitations))


class RealWorldClockTests(unittest.TestCase):
    def test_2027_recommendation_uses_post_promo_rates(self):
        """The suite's guarantee: no silent wrong numbers after 2027-01-01."""
        card = pulse_style_card()
        facts = {"applicant_age": 25, "annual_income_hkd": 150000,
                 "mainland_payment_method": "mobile_or_qr",
                 "monthly_card_spend_band_hkd": "20k_plus"}

        result = recommend([card], facts, ["mainland_china"], "no_preference",
                           now=AFTER_PROMO)

        self.assertEqual(Decimal("3440.00"),
                         result.primary.estimated_annual_reward_hkd)
        self.assertEqual([], result.primary.promo_rules)


if __name__ == "__main__":
    unittest.main()
