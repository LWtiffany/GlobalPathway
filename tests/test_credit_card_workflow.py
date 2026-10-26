import asyncio
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from agent2.orchestration.contracts import DomainTask, OrchestrationInput
from agent2.orchestration.domains.credit_card import CreditCardWorkflow, extract_credit_card_facts
from agent2.orchestration.domains.credit_card_engine import (
    assess_eligibility, needs_asset_information, recommend, score_product,
)
from agent2.orchestration.domains.credit_card_repository import CreditCardRepositoryError
from agent2.orchestration.domains.credit_card_models import (
    CardBenefit, CardBorrowingTerm, CardCatalogRead, CardFee, CreditCardProduct, EligibilityRule,
    RewardRule,
)
from agent2.knowledge import FixtureKnowledgeRead
from agent2.llm.factory import LlmComponents
from agent2.service import Agent2Service
from agent2.store import Store

NOW = datetime(2026, 10, 18, tzinfo=timezone.utc)


def product(card_id, category, rate, *, verified=True, income=100000, network="Visa"):
    return CreditCardProduct(
        card_id=card_id, issuing_bank="Test Bank", card_network=network, market="Hong Kong",
        product_name=card_id.replace("_", " ").title(),
        official_url=f"https://example.com/{card_id}" if verified else None,
        last_verified_at=NOW if verified else None,
        eligibility_rules=[
            EligibilityRule(rule_id=f"{card_id}-age", rule_type="minimum_age", operator="gte",
                            value_numeric=Decimal("18")),
            EligibilityRule(rule_id=f"{card_id}-income", rule_type="minimum_income", operator="gte",
                            value_numeric=Decimal(str(income)), currency="HKD"),
        ],
        reward_rules=[
            RewardRule(rule_id=f"{card_id}-base", spend_category="general", reward_type="cashback",
                       reward_rate=Decimal("0.004"),
                       source_url=f"https://example.com/{card_id}" if verified else None,
                       last_verified_at=NOW if verified else None),
            RewardRule(rule_id=f"{card_id}-special", spend_category=category, reward_type="cashback",
                       reward_rate=Decimal(str(rate)),
                       source_url=f"https://example.com/{card_id}" if verified else None,
                       last_verified_at=NOW if verified else None),
        ],
        fees=[CardFee(fee_id=f"{card_id}-annual", fee_type="annual_fee", amount=Decimal("0"),
                      currency="HKD", waiver_condition="No annual fee",
                      source_url=f"https://example.com/{card_id}" if verified else None,
                      last_verified_at=NOW if verified else None)],
        borrowing_terms=[
            CardBorrowingTerm(
                borrowing_term_id=f"{card_id}-purchase", term_type="purchase_apr",
                apr=Decimal("0.3542"), description="official", source_url="https://example.com/kfs",
                last_verified_at=NOW),
            CardBorrowingTerm(
                borrowing_term_id=f"{card_id}-free", term_type="interest_free_period",
                max_interest_free_days=56, description="official", source_url="https://example.com/kfs",
                last_verified_at=NOW),
            CardBorrowingTerm(
                borrowing_term_id=f"{card_id}-spending", term_type="spending_instalment",
                personalized_pricing=True, description="personalized",
                source_url="https://example.com/installment", last_verified_at=NOW),
            CardBorrowingTerm(
                borrowing_term_id=f"{card_id}-cash-offer", term_type="cash_instalment_offer",
                monthly_fee_rate=Decimal("0.0019"), personalized_pricing=True,
                description="as low as", source_url="https://example.com/installment",
                last_verified_at=NOW),
        ],
    )


def asset_review_product(card_id="asset_review_card", income=120000):
    card = product(card_id, "online", "0.04", income=income)
    return card.model_copy(update={"eligibility_rules": [
        card.eligibility_rules[0].model_copy(update={"path_id": "hard"}),
        card.eligibility_rules[1].model_copy(update={"path_id": "income"}),
        EligibilityRule(
            rule_id=f"{card_id}-asset-review", rule_type="asset_proof", operator="available",
            value_text="Recent bank-issued asset proof", path_id="asset_review",
            outcome_if_met="needs_bank_assessment",
            description="可提交最近3个月银行资产证明，由银行个案审批。",
        ),
    ]})


class FakeRepository:
    def __init__(self, products):
        self.products = products
        self.called = False

    async def active_products(self, market, now):
        self.called = True
        return CardCatalogRead(read_id="read-1", market=market, products=self.products)


class FailingRepository:
    """Simulates an RDS outage inside the domain boundary."""

    async def active_products(self, market, now):
        raise CreditCardRepositoryError("rds connection reset")


def request(**values):
    defaults = {"application_region": "CN-HK", "applicant_age": 25, "annual_income_hkd": 200000,
                "repayment_mode": "full", "spending_scenarios": "online,travel",
                "monthly_card_spend_band_hkd": "5k_10k",
                "reward_preference": "no_preference"}
    defaults.update(values)
    return OrchestrationInput(text="信用卡推荐", profile_id="p1", profile_version=2,
                              profile_values=defaults)


TASK = DomainTask(task_id="task-1", domain="credit_card", intent="credit_card")


class CreditCardWorkflowTests(unittest.TestCase):
    def test_collects_eligibility_before_usage(self):
        repo = FakeRepository([])
        result = asyncio.run(CreditCardWorkflow(repo, clock=lambda: NOW).run(
            TASK, OrchestrationInput(text="想办信用卡", profile_id="p1", profile_version=2)))

        self.assertEqual("needs_input", result.status)
        self.assertEqual(["application_region"],
                         [question.field for question in result.follow_up_questions])
        self.assertTrue(all(q.input_type == "single_select" for q in result.follow_up_questions))
        self.assertTrue(all(q.persist_mode == "save_on_select" for q in result.follow_up_questions))
        self.assertFalse(repo.called)

    def test_low_income_asks_for_asset_evidence_before_usage(self):
        result = asyncio.run(CreditCardWorkflow(
            FakeRepository([asset_review_product()]), clock=lambda: NOW).run(
            TASK, request(annual_income_hkd=30000, asset_band=None)))

        self.assertEqual("needs_input", result.status)
        self.assertEqual(["asset_band"], [question.field for question in result.follow_up_questions])
        self.assertIn("个案审批", result.follow_up_questions[0].text)

    def test_asset_evidence_produces_explicit_bank_assessment_result(self):
        result = asyncio.run(CreditCardWorkflow(
            FakeRepository([asset_review_product()]), clock=lambda: NOW).run(
            TASK, request(annual_income_hkd=30000, asset_band="under_100k")))

        self.assertEqual("answered", result.status)
        self.assertIn("可提交资产证明", "\n".join(result.answer.steps))
        self.assertIn("申请限制", "\n".join(result.answer.steps))
        recommendation = result.payload["recommendations"]["primary"]
        self.assertEqual("needs_bank_assessment", recommendation["eligibility"]["status"])

    def test_income_above_threshold_does_not_ask_for_assets(self):
        result = asyncio.run(CreditCardWorkflow(
            FakeRepository([asset_review_product()]), clock=lambda: NOW).run(
            TASK, request(annual_income_hkd=150000, repayment_mode=None,
                          spending_scenarios=None)))

        self.assertEqual("needs_input", result.status)
        self.assertEqual(
            ["repayment_mode"],
            [question.field for question in result.follow_up_questions],
        )

    def test_no_income_or_asset_route_explains_failed_threshold(self):
        result = asyncio.run(CreditCardWorkflow(
            FakeRepository([asset_review_product()]), clock=lambda: NOW).run(
            TASK, request(annual_income_hkd=30000, asset_band="none")))

        self.assertEqual("cannot_answer", result.status)
        self.assertIn("不满足", result.answer.steps[0])
        self.assertIn("银行资产证明", result.answer.steps[0])

    def test_carrying_balance_uses_sourced_borrowing_costs(self):
        repo = FakeRepository([product("online_card", "online", "0.04")])
        result = asyncio.run(CreditCardWorkflow(repo, clock=lambda: NOW).run(
            TASK, request(repayment_mode="sometimes_carry")))

        self.assertEqual("answered", result.status)
        self.assertIn("APR 35.42%", result.answer.steps[0])
        self.assertTrue(repo.called)

    def test_selects_primary_then_fills_a_real_coverage_gap(self):
        repo = FakeRepository([
            product("online_card", "online", "0.04"),
            product("travel_card", "travel_and_transport", "0.02", network="Mastercard"),
            product("general_card", "selected_retail", "0.01"),
        ])
        result = asyncio.run(CreditCardWorkflow(repo, clock=lambda: NOW).run(TASK, request()))
        payload = result.payload["recommendations"]

        self.assertEqual("answered", result.status)
        self.assertEqual("online_card", payload["primary"]["card_id"])
        self.assertEqual(["travel_card"], [item["card_id"] for item in payload["supplements"]])
        self.assertLessEqual(len(payload["alternatives"]), 3)

    def test_unverified_product_fields_are_never_a_definite_recommendation(self):
        repo = FakeRepository([product("demo_card", "online", "0.04", verified=False)])
        result = asyncio.run(CreditCardWorkflow(repo, clock=lambda: NOW).run(TASK, request()))

        self.assertEqual("needs_verification", result.status)
        self.assertIn("仍待官网核验", result.answer.limitations[0])

    def test_no_fee_alternatives_require_eligibility_and_are_capped(self):
        cards = [product(f"card_{index}", "online", f"0.0{index + 1}", income=100000)
                 for index in range(6)]
        cards.append(product("too_expensive", "online", "0.05", income=999999))
        result = recommend(cards, {"applicant_age": 25, "annual_income_hkd": 200000}, ["online"])

        self.assertLessEqual(len(result.alternatives), 3)
        self.assertIn("too_expensive", result.excluded)

    def test_age_only_question_when_region_and_income_already_known(self):
        repo = FakeRepository([])
        result = asyncio.run(CreditCardWorkflow(repo, clock=lambda: NOW).run(
            TASK, OrchestrationInput(text="", profile_id="p1", profile_version=2,
                                     profile_values={"application_region": "CN-HK",
                                                     "annual_income_hkd": 200000})))

        self.assertEqual("needs_input", result.status)
        self.assertEqual(["applicant_age_band"],
                         [question.field for question in result.follow_up_questions])
        self.assertFalse(repo.called)  # no catalogue fetch needed to ask age

    def test_usage_fields_are_asked_one_at_a_time(self):
        repo = FakeRepository([product("available_card", "online", "0.04")])
        result = asyncio.run(CreditCardWorkflow(repo, clock=lambda: NOW).run(
            TASK, OrchestrationInput(
                text="", profile_id="p1", profile_version=2,
                profile_values={"application_region": "CN-HK", "applicant_age": 25,
                                "annual_income_hkd": 200000})))

        self.assertEqual("needs_input", result.status)
        self.assertEqual(["repayment_mode"],
                         [question.field for question in result.follow_up_questions])
        self.assertEqual("single_select", result.follow_up_questions[0].input_type)
        self.assertTrue(repo.called)  # eligibility paths are checked before usage questions
        next_q = asyncio.run(CreditCardWorkflow(repo, clock=lambda: NOW).run(
            TASK, OrchestrationInput(
                text="", profile_id="p1", profile_version=2,
                profile_values={"application_region": "CN-HK", "applicant_age": 25,
                                "annual_income_hkd": 200000, "repayment_mode": "full"})))
        self.assertEqual(["spending_scenarios"],
                         [question.field for question in next_q.follow_up_questions])
        self.assertEqual("multi_select", next_q.follow_up_questions[0].input_type)
        self.assertEqual(3, next_q.follow_up_questions[0].max_selections)

    def test_incomplete_borrowing_cost_data_downgrades_to_verification(self):
        minimal = CreditCardProduct(
            card_id="minimal", issuing_bank="Test Bank", card_network="Visa",
            market="Hong Kong", product_name="Minimal Card",
            official_url="https://example.com/minimal", last_verified_at=NOW,
            eligibility_rules=[EligibilityRule(
                rule_id="minimal-age", rule_type="minimum_age", operator="gte",
                value_numeric=Decimal("18"))],
            borrowing_terms=[CardBorrowingTerm(
                borrowing_term_id="minimal-purchase", term_type="purchase_apr",
                apr=Decimal("0.30"), description="official",
                source_url="https://example.com/kfs", last_verified_at=NOW)],
        )
        result = asyncio.run(CreditCardWorkflow(
            FakeRepository([minimal]), clock=lambda: NOW).run(
            TASK, request(repayment_mode="sometimes_carry")))

        self.assertEqual("needs_verification", result.status)
        self.assertIn("borrowing_cost_data_missing",
                      [notice.code for notice in result.notices])
        self.assertIn("借款成本", result.answer.summary)

    def test_repository_failure_becomes_verification_not_crash(self):
        result = asyncio.run(CreditCardWorkflow(
            FailingRepository(), clock=lambda: NOW).run(
            TASK, request(annual_income_hkd=None)))

        self.assertEqual("needs_verification", result.status)
        self.assertEqual("credit_card_repository_unavailable",
                         result.notices[0].code)

    def test_empty_catalog_is_cannot_answer(self):
        result = asyncio.run(CreditCardWorkflow(
            FakeRepository([]), clock=lambda: NOW).run(
            TASK, request(annual_income_hkd=None)))

        self.assertEqual("cannot_answer", result.status)
        self.assertIn("没有该地区当前有效", result.answer.summary)

    def test_all_ineligible_products_are_cannot_answer(self):
        result = asyncio.run(CreditCardWorkflow(
            FakeRepository([product("rich", "online", "0.04", income=999999)]),
            clock=lambda: NOW).run(TASK, request()))

        self.assertEqual("cannot_answer", result.status)
        self.assertIn("不满足你的申请门槛", result.answer.summary)

    def test_asset_proof_path_keeps_low_income_card_in_recommendations(self):
        card = asset_review_product()

        result = recommend(
            [card], {"applicant_age": 20, "annual_income_hkd": 30000,
                     "asset_proof_available": True}, ["online"])

        self.assertEqual("asset_review_card", result.primary.card_id)
        self.assertEqual("needs_bank_assessment", result.primary.eligibility.status)
        self.assertEqual("asset_review", result.primary.eligibility.matched_path)


class MarketRepository:
    def __init__(self, by_market):
        self.by_market = by_market

    async def active_products(self, market, now):
        return CardCatalogRead(read_id=f"read-{market}", market=market,
                               products=self.by_market.get(market, []))


class StudyDestinationCatalogTests(unittest.TestCase):
    def test_us_study_only_lists_cards_from_selected_market(self):
        local = product("premier_world", "travel", "0.01")
        local = local.model_copy(update={
            "market": "US",
            "product_name": "HSBC Premier World Mastercard",
            "card_network": "Mastercard",
            "fees": [CardFee(fee_id="us-annual", fee_type="annual_fee", amount=Decimal("95"),
                             currency="USD", source_url="https://example.com/us", last_verified_at=NOW),
                     CardFee(fee_id="us-fx", fee_type="foreign_transaction_fee", rate=Decimal("0"),
                             currency="USD", source_url="https://example.com/us", last_verified_at=NOW)],
            "eligibility_rules": [
                EligibilityRule(rule_id="us-rel", rule_type="banking_relationship", operator="eq",
                                value_text="us_premier_checking", required=True),
            ],
        })
        hong_kong = product("hk_red", "online", "0.04")
        repo = MarketRepository({"US": [local], "CN-HK": [hong_kong]})

        result = asyncio.run(CreditCardWorkflow(repo, clock=lambda: NOW).run(
            TASK, request(application_region="US",
                          us_hsbc_account_path="not_applicable",
                          spending_scenarios=["overseas", "tuition"],
                          reward_preference="no_preference")))

        self.assertIn(result.status, {"answered", "needs_verification"})
        text = "\n".join(result.answer.steps)
        self.assertIn("汇丰美国发行的卡", text)
        self.assertNotIn("汇丰香港发行的卡", text)
        self.assertIn("美国汇丰 Premier 支票账户", text)
        self.assertIn("HSBC Premier World Mastercard", text)
        self.assertIn("申请限制：目前不符合该卡公开申请条件", text)
        self.assertNotIn("Hk Red", text)
        self.assertIn("年费：", text)
        self.assertIn("主要权益：", text)
        self.assertNotIn("备用", result.answer.summary)
        us_fee = next(line for line in result.answer.steps if line.startswith("年费：") and "95 USD" in line)
        self.assertNotIn("HK$", us_fee)
        self.assertEqual(["US"], [group["market"] for group in result.payload["groups"]])
        self.assertEqual(["primary"], [group["role"] for group in result.payload["groups"]])
        self.assertTrue(any("不混入其他地区" in item for item in result.answer.limitations))
        self.assertNotIn("产品库", "\n".join(result.answer.steps))
        self.assertNotIn("门槛依据", "\n".join(result.answer.steps))

    def test_selected_market_cards_use_spending_scenarios_for_ordering(self):
        travel = product("a_travel", "overseas", "0.04").model_copy(update={"market": "US"})
        online = product("z_online", "online", "0.04").model_copy(update={"market": "US"})
        result = asyncio.run(CreditCardWorkflow(
            MarketRepository({"US": [travel, online], "CN-HK": []}), clock=lambda: NOW).run(
            TASK, request(application_region="US", applicant_age=20,
                          us_hsbc_account_path="not_applicable",
                          spending_scenarios=["online"], reward_preference="cashback")))

        names = [card["product_name"] for card in result.payload["groups"][0]["cards"]]
        self.assertEqual(["Z Online", "A Travel"], names)

    def test_us_study_skips_account_question_and_asks_only_card_use(self):
        local = product("premier_world", "travel", "0.01").model_copy(update={
            "market": "US",
            "eligibility_rules": [
                EligibilityRule(rule_id="us-rel", rule_type="banking_relationship", operator="eq",
                                value_text="us_premier_checking", required=True),
            ],
        })
        result = asyncio.run(CreditCardWorkflow(
            MarketRepository({"US": [local], "CN-HK": [product("hk_red", "online", "0.04")]}),
            clock=lambda: NOW).run(
            TASK, OrchestrationInput(
                text="想办信用卡", profile_id="p1", profile_version=1,
                profile_values={"application_region": "US", "applicant_age": 20})))

        self.assertEqual("needs_input", result.status)
        self.assertEqual(["spending_scenarios"],
                         [q.field for q in result.follow_up_questions])
        self.assertEqual(2, result.follow_up_questions[0].max_selections)

    def test_uk_study_asks_uk_account_path(self):
        result = asyncio.run(CreditCardWorkflow(
            MarketRepository({"UK": [product("uk_student", "overseas", "0.01").model_copy(update={
                "market": "UK",
                "eligibility_rules": [
                    EligibilityRule(rule_id="uk-stu", rule_type="banking_relationship",
                                    operator="eq", value_text="uk_student_account", required=True),
                ],
            })], "CN-HK": []}),
            clock=lambda: NOW).run(
            TASK, OrchestrationInput(
                text="信用卡", profile_id="p1", profile_version=1,
                profile_values={"destination_country": "UK", "applicant_age": 19})))

        self.assertEqual("needs_input", result.status)
        self.assertEqual(["uk_hsbc_account_path"],
                         [q.field for q in result.follow_up_questions])
        self.assertEqual(
            ["premier_account", "plan_to_open"],
            [option["value"] for option in result.follow_up_questions[0].options])

    def test_uk_non_premier_path_recommends_student_card_without_more_questions(self):
        student = product("student", "general", "0").model_copy(update={
            "market": "UK", "product_name": "HSBC Student Credit Card", "card_tier": "Student",
            "eligibility_rules": [EligibilityRule(
                rule_id="student-account", rule_type="banking_relationship", operator="eq",
                value_text="uk_student_account", required=True)],
        })
        premier = product("premier", "general", "0.01").model_copy(update={
            "market": "UK", "product_name": "HSBC Premier Credit Card", "card_tier": "Premier",
            "eligibility_rules": [EligibilityRule(
                rule_id="premier-account", rule_type="banking_relationship", operator="eq",
                value_text="uk_premier_account", required=True)],
        })
        result = asyncio.run(CreditCardWorkflow(
            MarketRepository({"UK": [premier, student]}), clock=lambda: NOW).run(
            TASK, request(application_region="UK", uk_hsbc_account_path="plan_to_open")))

        self.assertNotEqual("needs_input", result.status)
        self.assertEqual("HSBC Student Credit Card",
                         result.payload["groups"][0]["cards"][0]["product_name"])
        self.assertNotIn("HSBC Hong Kong", "\n".join(result.answer.steps))

    def test_destination_country_selects_the_study_catalog(self):
        result = asyncio.run(CreditCardWorkflow(
            MarketRepository({"UK": [], "CN-HK": []}), clock=lambda: NOW).run(
            TASK, OrchestrationInput(
                text="信用卡", profile_id="p1", profile_version=1,
                profile_values={"destination_country": "UK", "applicant_age": 19,
                                "uk_hsbc_account_path": "plan_to_open",
                                "spending_scenarios": ["overseas"],
                                "reward_preference": "no_preference"})))

        self.assertEqual("cannot_answer", result.status)
        self.assertIn("没有该地区", result.answer.summary)

    def test_hong_kong_destination_skips_region_question(self):
        result = asyncio.run(CreditCardWorkflow(
            FakeRepository([product("hk_red", "online", "0.04")]), clock=lambda: NOW).run(
            TASK, OrchestrationInput(
                text="想办信用卡", profile_id="p1", profile_version=1,
                profile_values={"destination_country": "CN-HK"})))

        self.assertEqual("needs_input", result.status)
        self.assertEqual(["applicant_age_band"],
                         [q.field for q in result.follow_up_questions])
        self.assertNotIn("application_region",
                         [q.field for q in result.follow_up_questions])


class CreditCardFactExtractionTests(unittest.TestCase):
    def test_extracts_region_income_repayment_scenarios_and_reward(self):
        facts, basis = extract_credit_card_facts(
            "我在香港想办信用卡，年薪30万，每月全额还款，主要网购和旅行，想要返现")

        self.assertEqual("CN-HK", facts["application_region"])
        self.assertEqual(300000, facts["annual_income_hkd"])
        self.assertEqual("full", facts["repayment_mode"])
        self.assertEqual(["online", "travel"], facts["spending_scenarios"])
        self.assertEqual("cashback", facts["reward_preference"])
        self.assertEqual("香港", basis["application_region"])

    def test_income_band_and_carry_repayment_are_detected(self):
        facts, _ = extract_credit_card_facts("年薪15万，通常分期还款，境外和内地消费")

        self.assertEqual(150000, facts["annual_income_hkd"])
        self.assertEqual("usually_carry", facts["repayment_mode"])
        self.assertEqual(["overseas", "mainland_china"], facts["spending_scenarios"])

    def test_region_requires_credit_card_intent_nearby(self):
        facts, _ = extract_credit_card_facts("我在香港工作生活，收入稳定")

        self.assertNotIn("application_region", facts)


class CreditCardEngineEdgeCaseTests(unittest.TestCase):
    def test_generic_online_spend_does_not_match_selected_retail_programme(self):
        card = product("selected_retail_card", "selected_retail", "0.05")
        eligibility = assess_eligibility(card, {"applicant_age": 25, "annual_income_hkd": 200000})

        scored = score_product(card, eligibility, ["online"], "no_preference")

        self.assertEqual([], scored.matched_scenarios)

    def test_pulse_style_card_beats_easy_for_ranked_online_mainland_and_travel_use(self):
        easy = product("hsbc_easy", "selected_retail", "0.04")
        easy = easy.model_copy(update={
            "reward_rules": [
                RewardRule(rule_id="easy-general", spend_category="general", reward_type="points",
                           points_per_currency_unit=Decimal("1")),
                RewardRule(rule_id="easy-retail", spend_category="selected_retail", reward_type="points",
                           points_per_currency_unit=Decimal("6")),
            ],
            "fees": [
                CardFee(fee_id="easy-annual", fee_type="annual_fee", amount=Decimal("0"),
                        currency="HKD", waiver_condition="Annual fee waived"),
                CardFee(fee_id="easy-fx", fee_type="foreign_transaction_fee",
                        rate=Decimal("0.0195")),
            ],
        })
        pulse = product("hsbc_pulse", "mainland_china_and_macau", "0.044")
        pulse = pulse.model_copy(update={
            "fees": [
                CardFee(fee_id="pulse-annual", fee_type="annual_fee", amount=Decimal("1800"),
                        currency="HKD", waiver_condition="First two years annual fee waiver"),
                CardFee(fee_id="pulse-fx", fee_type="foreign_transaction_fee", rate=Decimal("0")),
            ],
            "benefits": [
                CardBenefit(benefit_id="pulse-dual", benefit_type="dual_currency",
                            benefit_name="HKD and RMB dual currency"),
                CardBenefit(benefit_id="pulse-mainland", benefit_type="merchant_offer",
                            benefit_name="Mainland China and Macau offers"),
            ],
        })

        result = recommend(
            [easy, pulse], {"applicant_age": 25, "annual_income_hkd": 240000},
            ["online", "mainland_china", "travel"],
        )

        self.assertEqual("hsbc_pulse", result.primary.card_id)
        self.assertEqual(["hsbc_pulse", "hsbc_easy"],
                         [card.card_id for card in result.ranked_candidates])
        self.assertEqual(Decimal("0.00"),
                         result.primary.score_breakdown["annual_fee_hkd"])
        self.assertEqual(Decimal("0.00"),
                         result.primary.score_breakdown["annual_fx_cost_hkd"])

    def test_scenario_selection_order_changes_priority(self):
        online = product("online_card", "online", "0.05")
        mainland = product("mainland_card", "mainland_china", "0.05")
        facts = {"applicant_age": 25, "annual_income_hkd": 240000}

        online_first = recommend([online, mainland], facts, ["online", "mainland_china"])
        mainland_first = recommend([online, mainland], facts, ["mainland_china", "online"])

        self.assertEqual("online_card", online_first.primary.card_id)
        self.assertEqual("mainland_card", mainland_first.primary.card_id)

    def test_hong_kong_residency_is_a_structured_eligibility_fact(self):
        card = product("resident_card", "online", "0.04")
        card = card.model_copy(update={"eligibility_rules": list(card.eligibility_rules) + [
            EligibilityRule(rule_id="resident", rule_type="residency", operator="eq",
                            value_text="Hong Kong resident"),
        ]})

        missing = assess_eligibility(card, {"applicant_age": 25, "annual_income_hkd": 200000})
        passed = assess_eligibility(
            card, {"applicant_age": 25, "annual_income_hkd": 200000, "hk_resident": True})

        self.assertIn("hk_resident", missing.missing_fields)
        self.assertEqual("eligible", passed.status)

    def test_non_resident_is_recommended_with_low_approval_disclosure(self):
        card = product("resident_card", "online", "0.04")
        card = card.model_copy(update={"eligibility_rules": list(card.eligibility_rules) + [
            EligibilityRule(rule_id="resident", rule_type="residency", operator="eq",
                            value_text="Hong Kong resident"),
        ]})

        assessment = assess_eligibility(
            card, {"applicant_age": 25, "annual_income_hkd": 200000, "hk_resident": False})

        # 明文条件未满足，但不硬拒：降级为个案审批并标记低通过率。
        self.assertEqual("needs_bank_assessment", assessment.status)
        self.assertTrue(assessment.low_approval_chance)
        self.assertTrue(any("非香港居民" in reason for reason in assessment.reasons))
        self.assertTrue(any("低几率" in reason for reason in assessment.reasons))

        result = recommend([card], {"applicant_age": 25, "annual_income_hkd": 200000,
                                    "hk_resident": False,
                                    "monthly_card_spend_band_hkd": "5k_10k"}, ["online"])
        self.assertIsNotNone(result.primary)
        self.assertEqual("needs_bank_assessment", result.primary.eligibility.status)

    def test_non_resident_downgrades_passed_income_path_to_review(self):
        card = asset_review_product()
        card = card.model_copy(update={"eligibility_rules": list(card.eligibility_rules) + [
            EligibilityRule(rule_id="resident", rule_type="residency", operator="eq",
                            value_text="Hong Kong resident", path_id="hard"),
        ]})

        non_resident = assess_eligibility(
            card, {"applicant_age": 25, "annual_income_hkd": 200000, "hk_resident": False})
        resident = assess_eligibility(
            card, {"applicant_age": 25, "annual_income_hkd": 200000, "hk_resident": True})

        # 收入路径已通过也只按个案审批展示；居民身份正常时同一张卡 eligible。
        self.assertEqual("needs_bank_assessment", non_resident.status)
        self.assertEqual("income", non_resident.matched_path)
        self.assertTrue(non_resident.low_approval_chance)
        self.assertEqual("eligible", resident.status)
        self.assertFalse(resident.low_approval_chance)

    def test_non_resident_below_income_threshold_stays_ineligible(self):
        # 软门槛只放宽居民身份这一项；其余成文条件（收入路径）失败仍然出局。
        card = product("resident_card", "online", "0.04", income=100000)
        card = card.model_copy(update={"eligibility_rules": list(card.eligibility_rules) + [
            EligibilityRule(rule_id="resident", rule_type="residency", operator="eq",
                            value_text="Hong Kong resident"),
        ]})

        assessment = assess_eligibility(
            card, {"applicant_age": 25, "annual_income_hkd": 50000, "hk_resident": False})

        self.assertEqual("ineligible", assessment.status)

    def test_premier_relationship_balance_band_is_not_inferred_from_income(self):
        card = product("premier_card", "travel_and_dining", "0.024")
        card = card.model_copy(update={"eligibility_rules": list(card.eligibility_rules) + [
            EligibilityRule(rule_id="premier-status", rule_type="banking_relationship",
                            operator="required", path_id="premier"),
            EligibilityRule(rule_id="premier-balance", rule_type="relationship_balance",
                            operator="gte", value_numeric=Decimal("100000"), currency="HKD",
                            path_id="premier"),
        ]})
        income_only = assess_eligibility(
            card, {"applicant_age": 25, "annual_income_hkd": 2000000})
        existing_premier = assess_eligibility(card, {
            "applicant_age": 25,
            "annual_income_hkd": 200000,
            "premier_relationship": True,
            "hsbc_relationship_balance_band": "1m_plus",
        })

        self.assertEqual("unknown", income_only.status)
        self.assertIn("premier_relationship", income_only.missing_fields)
        self.assertEqual("eligible", existing_premier.status)

    def test_one_million_trb_path_is_a_conditional_premier_route(self):
        card = product("premier_card", "travel_and_dining", "0.024")
        card = card.model_copy(update={"eligibility_rules": list(card.eligibility_rules) + [
            EligibilityRule(rule_id="premier-status", rule_type="banking_relationship",
                            operator="required", path_id="premier"),
            EligibilityRule(rule_id="premier-balance", rule_type="relationship_balance",
                            operator="gte", value_numeric=Decimal("100000"), currency="HKD",
                            path_id="premier"),
            EligibilityRule(rule_id="qualify-premier", rule_type="relationship_balance",
                            operator="gte", value_numeric=Decimal("1000000"), currency="HKD",
                            path_id="qualify_premier", outcome_if_met="needs_bank_assessment"),
        ]})

        assessment = assess_eligibility(card, {
            "applicant_age": 25,
            "annual_income_hkd": 200000,
            "hsbc_premier_path": "can_qualify_with_1m_trb",
        })

        self.assertEqual("needs_bank_assessment", assessment.status)
        self.assertEqual("qualify_premier", assessment.matched_path)

    def test_premier_question_exposes_one_million_trb_as_a_separate_choice(self):
        card = product("premier_card", "travel_and_dining", "0.024")
        card = card.model_copy(update={"eligibility_rules": list(card.eligibility_rules) + [
            EligibilityRule(rule_id="premier-status", rule_type="banking_relationship",
                            operator="required", path_id="premier"),
        ]})

        result = asyncio.run(CreditCardWorkflow(
            FakeRepository([card]), clock=lambda: NOW).run(TASK, request()))

        self.assertEqual(["hsbc_premier_path"],
                         [question.field for question in result.follow_up_questions])
        labels = [option["label"] for option in result.follow_up_questions[0].options]
        self.assertTrue(any("HK$1,000,000" in label for label in labels))

    def test_selected_reward_category_is_only_used_for_one_scenario(self):
        card = product("selectable", "selected_lifestyle", "0.024")
        card = card.model_copy(update={"reward_rules": [
            card.reward_rules[0],
            card.reward_rules[1].model_copy(
                update={"qualification_type": "selected_category"}),
        ]})
        eligibility = assess_eligibility(
            card, {"applicant_age": 25, "annual_income_hkd": 200000})

        scored = score_product(
            card, eligibility, ["dining", "travel", "retail"], "no_preference",
            Decimal("10000"))

        # 50% gets 2.4%; the remaining 50% stays at the 0.4% base rate.
        self.assertEqual(Decimal("1680.00"), scored.estimated_annual_reward_hkd)
        self.assertEqual(["dining"], scored.matched_scenarios)

    def test_pulse_headline_rate_requires_mainland_mobile_or_qr_payment(self):
        card = product("pulse", "mainland_china_and_macau", "0.044")
        card = card.model_copy(update={"reward_rules": [
            card.reward_rules[0],
            card.reward_rules[1].model_copy(update={
                "qualification_type": "payment_method",
                "qualification_value": "mobile_or_qr",
            }),
        ]})
        eligibility = assess_eligibility(
            card, {"applicant_age": 25, "annual_income_hkd": 200000})

        physical = score_product(
            card, eligibility, ["mainland_china"], "no_preference",
            Decimal("5000"), facts={"mainland_payment_method": "physical_card"})
        mobile = score_product(
            card, eligibility, ["mainland_china"], "no_preference",
            Decimal("5000"), facts={"mainland_payment_method": "mobile_or_qr"})

        self.assertEqual(Decimal("240.00"), physical.estimated_annual_reward_hkd)
        self.assertEqual(Decimal("2640.00"), mobile.estimated_annual_reward_hkd)

    def test_spend_above_reward_cap_falls_back_to_base_rate(self):
        card = product("capped", "online", "0.04")
        card = card.model_copy(update={"reward_rules": [
            card.reward_rules[0],
            card.reward_rules[1].model_copy(update={
                "spend_cap": Decimal("10000"), "cap_currency": "HKD",
                "cap_period": "month",
            }),
        ]})
        eligibility = assess_eligibility(
            card, {"applicant_age": 25, "annual_income_hkd": 200000})

        scored = score_product(
            card, eligibility, ["online"], "no_preference", Decimal("15000"))

        self.assertEqual(Decimal("5040.00"), scored.estimated_annual_reward_hkd)

    def test_monthly_reward_cap_is_used_in_annual_value(self):
        card = product("capped", "online", "0.10")
        card = card.model_copy(update={"reward_rules": [RewardRule(
            rule_id="capped-online", spend_category="online", reward_type="cashback",
            reward_rate=Decimal("0.10"), cash_equivalent_rate=Decimal("0.10"),
            spend_cap=Decimal("1000"), cap_currency="HKD", cap_period="month",
        )]})
        eligibility = assess_eligibility(card, {"applicant_age": 25, "annual_income_hkd": 200000})

        scored = score_product(
            card, eligibility, ["online"], "no_preference", Decimal("7500"))

        self.assertEqual(Decimal("1200.00"), scored.estimated_annual_reward_hkd)

    def test_explicit_miles_preference_uses_reward_units(self):
        cash = product("cash", "travel_and_transport", "0.05")
        miles = product("miles", "travel_and_transport", "0.00")
        miles = miles.model_copy(update={"reward_rules": [RewardRule(
            rule_id="miles-travel", spend_category="travel_and_transport", reward_type="miles",
            points_per_currency_unit=Decimal("0.5"),
        )]})
        facts = {"applicant_age": 25, "annual_income_hkd": 240000,
                 "monthly_card_spend_band_hkd": "5k_10k"}

        result = recommend([cash, miles], facts, ["travel"], "miles")

        self.assertEqual("miles", result.primary.card_id)
        self.assertGreater(result.primary.reward_units["miles"], 0)

    def test_existing_premier_customer_is_asked_for_relationship_balance(self):
        card = product("premier_card", "travel_and_dining", "0.024")
        card = card.model_copy(update={"eligibility_rules": list(card.eligibility_rules) + [
            EligibilityRule(rule_id="premier-status", rule_type="banking_relationship",
                            operator="required", path_id="premier"),
            EligibilityRule(rule_id="premier-balance", rule_type="relationship_balance",
                            operator="gte", value_numeric=Decimal("100000"), currency="HKD",
                            path_id="premier"),
        ]})

        result = asyncio.run(CreditCardWorkflow(
            FakeRepository([card]), clock=lambda: NOW).run(
            TASK, request(premier_relationship=True)))

        self.assertEqual("needs_input", result.status)
        self.assertEqual(["hsbc_relationship_balance_band"],
                         [question.field for question in result.follow_up_questions])

    def test_asset_question_only_when_income_path_is_definitely_below_threshold(self):
        card = asset_review_product(income=120000)

        self.assertTrue(needs_asset_information(
            card, {"annual_income_band_hkd": "lt_60000"}))
        self.assertFalse(needs_asset_information(
            card, {"annual_income_band_hkd": "gte_120000"}))
        self.assertFalse(needs_asset_information(
            card, {"annual_income_band_hkd": "lt_60000", "asset_band": "under_100k"}))

    def test_passed_income_path_does_not_report_missing_asset_path(self):
        card = asset_review_product(income=120000)

        assessment = assess_eligibility(
            card, {"applicant_age": 25, "annual_income_hkd": 240000})

        self.assertEqual("eligible", assessment.status)
        self.assertEqual([], assessment.missing_fields)

    def test_income_band_overlapping_threshold_marks_unknown_not_failed(self):
        card = product("band_card", "online", "0.04", income=150000)
        result = recommend(
            [card], {"applicant_age": 25, "annual_income_band_hkd": "range_100000_199999"},
            ["online"])

        self.assertIsNotNone(result.primary)
        self.assertEqual("unknown", result.primary.eligibility.status)

    def test_zero_fx_fee_rewards_overseas_spend(self):
        card = product("fx_zero", "overseas", "0.04")
        card = card.model_copy(update={"fees": list(card.fees) + [CardFee(
            fee_id="fx0", fee_type="foreign_transaction_fee", rate=Decimal("0"),
            currency="HKD")]})
        eligibility = assess_eligibility(card, {"applicant_age": 25, "annual_income_hkd": 200000})
        scored = score_product(card, eligibility, ["overseas"], "no_preference")

        self.assertEqual(Decimal("0.00"), scored.score_breakdown["annual_fx_cost_hkd"])

    def test_high_fx_fee_penalizes_overseas_spend(self):
        card = product("fx_high", "overseas", "0.04")
        card = card.model_copy(update={"fees": [CardFee(
            fee_id="fx3", fee_type="foreign_transaction_fee", rate=Decimal("0.03"),
            currency="HKD")]})
        eligibility = assess_eligibility(card, {"applicant_age": 25, "annual_income_hkd": 200000})
        scored = score_product(card, eligibility, ["overseas"], "no_preference")

        self.assertEqual(Decimal("-1800.00"), scored.score_breakdown["annual_fx_cost_hkd"])

    def test_travel_spend_includes_foreign_transaction_cost(self):
        card = product("travel_fx", "travel_and_transport", "0.04")
        card = card.model_copy(update={"fees": [CardFee(
            fee_id="fx195", fee_type="foreign_transaction_fee", rate=Decimal("0.0195"),
            currency="HKD")]})
        eligibility = assess_eligibility(
            card, {"applicant_age": 25, "annual_income_hkd": 200000})

        scored = score_product(
            card, eligibility, ["travel"], "no_preference", Decimal("5000"))

        self.assertEqual(Decimal("-1170.00"),
                         scored.score_breakdown["annual_fx_cost_hkd"])

    def test_reward_preference_does_not_change_cash_value_estimate(self):
        card = product("cash_card", "online", "0.04")
        eligibility = assess_eligibility(card, {"applicant_age": 25, "annual_income_hkd": 200000})
        base = score_product(card, eligibility, ["online"], "no_preference")
        bonus = score_product(card, eligibility, ["online"], "cashback")

        self.assertEqual(bonus.score, base.score)


class CreditCardServiceIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.store = Store()
        self.addCleanup(self.store.close)
        self.repository = FakeRepository([product("online_card", "online", "0.04")])
        self.service = Agent2Service(self.store, FixtureKnowledgeRead.from_file(),
                                     clock=lambda: NOW, credit_card_repository=self.repository,
                                     llm_components=LlmComponents())
        self.user = self.service.demo_login("credit-card-test")["user_id"]
        profile = self.service.create_profile(self.user, synthetic=True)
        self.conversation = self.service.create_conversation(self.user, profile.profile_id)["conversation_id"]

    def ask(self, text, key):
        return self.service.post_message(self.user, self.conversation, text, key)

    def test_free_text_cannot_bypass_structured_follow_up(self):
        first = self.ask("想申请香港信用卡", "m1")
        self.assertEqual("needs_input", first.status)
        self.assertEqual(["applicant_age_band"],
                         [item.field for item in first.follow_up_questions])

        second = self.ask("25岁，年收入20万港币", "m2")
        self.assertEqual("needs_input", second.status)
        self.assertEqual(["applicant_age_band"],
                         [item.field for item in second.follow_up_questions])
        profile_id = self.service._owned_conversation(self.user, self.conversation)["profile_id"]
        proposal_fields = {item.field_code for item in self.service.list_proposals(self.user, profile_id)}
        self.assertEqual({"application_region"}, proposal_fields)
        self.assertEqual(1, self.service.get_profile(self.user, profile_id).version)

    def test_structured_follow_up_choices_are_saved_to_profile_atomically(self):
        first = self.ask("想申请信用卡", "choice-1")
        self.assertEqual(["application_region"],
                         [q.field for q in first.follow_up_questions])
        region = self.service.post_message(
            self.user, self.conversation, "申请地区：中国香港", "choice-2a",
            confirmed_fields={"application_region": "CN-HK"}, expected_profile_version=1)
        self.assertEqual(["applicant_age_band"], [q.field for q in region.follow_up_questions])
        second = self.service.post_message(
            self.user, self.conversation, "年龄区间：18 岁或以上", "choice-2b",
            confirmed_fields={"applicant_age_band": "18_or_over"}, expected_profile_version=2)
        self.assertEqual(["annual_income_band_hkd"], [q.field for q in second.follow_up_questions])
        income_value = second.follow_up_questions[0].options[-1]["value"]
        third = self.service.post_message(
            self.user, self.conversation, "税前年收入区间：最高档", "choice-3",
            confirmed_fields={"annual_income_band_hkd": income_value}, expected_profile_version=3)
        self.assertEqual(["repayment_mode"],
                         [q.field for q in third.follow_up_questions])
        repay = self.service.post_message(
            self.user, self.conversation, "每月全额还款", "choice-4a",
            confirmed_fields={"repayment_mode": "full"}, expected_profile_version=4)
        self.assertEqual(["spending_scenarios"], [q.field for q in repay.follow_up_questions])
        fourth = self.service.post_message(
            self.user, self.conversation, "主要消费场景：网购、境外消费、内地消费", "choice-4b",
            confirmed_fields={"spending_scenarios": ["online", "overseas", "mainland_china"]},
            expected_profile_version=5)
        self.assertEqual(["mainland_payment_method"],
                         [q.field for q in fourth.follow_up_questions])
        pay = self.service.post_message(
            self.user, self.conversation, "主要使用手机支付或银联二维码", "choice-4c",
            confirmed_fields={"mainland_payment_method": "mobile_or_qr"},
            expected_profile_version=6)
        self.assertEqual(["monthly_card_spend_band_hkd"],
                         [q.field for q in pay.follow_up_questions])
        fifth = self.service.post_message(
            self.user, self.conversation, "每月消费约七千", "choice-5",
            confirmed_fields={"monthly_card_spend_band_hkd": "5k_10k"},
            expected_profile_version=7)
        self.assertEqual(["reward_preference"],
                         [q.field for q in fifth.follow_up_questions])
        final = self.service.post_message(
            self.user, self.conversation, "按预计价值比较", "choice-6",
            confirmed_fields={"reward_preference": "no_preference"},
            expected_profile_version=8)
        self.assertEqual("answered", final.status)
        self.assertEqual([], final.follow_up_questions)
        profile_id = self.service._owned_conversation(self.user, self.conversation)["profile_id"]
        profile = self.service.get_profile(self.user, profile_id)
        self.assertEqual(9, profile.version)
        for code in ("application_region", "applicant_age_band", "annual_income_band_hkd",
                     "repayment_mode", "spending_scenarios", "monthly_card_spend_band_hkd",
                     "mainland_payment_method", "reward_preference"):
            self.assertEqual("user_selection", profile.fields[code].source_type)
            self.assertEqual("confirmed", profile.fields[code].status)
        events = self.store.all("SELECT field_code FROM profile_field_events WHERE profile_id=?",
                                (profile_id,))
        self.assertEqual(8, len(events))
        self.assertEqual([], self.service.list_proposals(self.user, profile_id))
        self.assertTrue(self.repository.called)
        advice = self.service.get_advice(self.user, profile_id, "credit_card")
        self.assertEqual("online_card", advice["payload"]["recommendations"]["primary"]["card_id"])
        run = self.store.one("SELECT prompt_version, gate_version FROM answer_runs WHERE message_id=?",
                             (final.message_id,))
        self.assertEqual((run["prompt_version"], run["gate_version"]),
                         ("credit-card-template-1", "orchestration-gate-1"))


class ProfileSchemaTests(unittest.TestCase):
    def test_schema_separates_auth_sessions_from_one_to_one_chat_sessions(self):
        store = Store()
        self.addCleanup(store.close)
        tables = {row["name"] for row in store.all(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertTrue({"auth_sessions", "sessions", "profile_field_values",
                         "profile_field_definitions", "profile_field_events"} <= tables)
        session_columns = {row["name"] for row in store.all("PRAGMA table_info(sessions)")}
        self.assertIn("session_id", session_columns)
        self.assertNotIn("token", session_columns)

    def test_legacy_profile_fields_are_migrated(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "legacy.sqlite3"
            db = sqlite3.connect(path)
            db.executescript("""
                CREATE TABLE users (user_id TEXT PRIMARY KEY, external_subject TEXT NOT NULL UNIQUE,
                    display_name TEXT NOT NULL, synthetic INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'active');
                CREATE TABLE sessions (token TEXT PRIMARY KEY, user_id TEXT NOT NULL, created_at TEXT NOT NULL);
                CREATE TABLE profiles (profile_id TEXT PRIMARY KEY, user_id TEXT NOT NULL, version INTEGER NOT NULL,
                    synthetic INTEGER NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
                CREATE TABLE profile_fields (profile_id TEXT NOT NULL, field_code TEXT NOT NULL, value_json TEXT,
                    status TEXT NOT NULL, source TEXT, confirmed_at TEXT, PRIMARY KEY(profile_id, field_code));
                CREATE TABLE conversations (conversation_id TEXT PRIMARY KEY, user_id TEXT NOT NULL,
                    profile_id TEXT NOT NULL, profile_version INTEGER NOT NULL, created_at TEXT NOT NULL);
                INSERT INTO users VALUES ('u1','demo:u1','U1',1,'active');
                INSERT INTO profiles VALUES ('p1','u1',1,1,'2026-01-01','2026-01-01');
                INSERT INTO profile_fields VALUES ('p1','repayment_mode','\"full\"','confirmed','user','2026-01-01');
                INSERT INTO conversations VALUES ('c1','u1','p1',1,'2026-01-01');
            """)
            db.commit()
            db.close()
            store = Store(path)
            try:
                migrated = store.one("SELECT * FROM profile_field_values WHERE profile_id='p1' "
                                     "AND field_code='repayment_mode'")
                self.assertEqual((migrated["source_type"], migrated["field_version"]), ("user_edit", 1))
                self.assertEqual("c1", store.one(
                    "SELECT session_id FROM sessions WHERE profile_id='p1'")["session_id"])
            finally:
                store.close()


def student_card_product():
    card = product("student_card", "general", "0.004", income=0)
    return card.model_copy(update={"eligibility_rules": [
        EligibilityRule(rule_id="student-age", rule_type="minimum_age", operator="gte",
                        value_numeric=Decimal("18")),
        EligibilityRule(rule_id="student-status", rule_type="student_status", operator="eq",
                        value_text="Full-time student in Hong Kong"),
        EligibilityRule(rule_id="student-institution", rule_type="institution_status",
                        operator="eq", value_text="HSBC listed institution"),
    ]})


class CreditCardStudentServiceIntegrationTests(unittest.TestCase):
    """学生路径必须走通 service 层的档案字段校验。

    回归背景：workflow 直调测试全绿，但 ``student_institution_eligible`` 曾漏登记在
    clarify.FIELDS，真实链路（service.post_message 的字段校验）直接抛 ServiceError，
    学生路径整体不可用。这两个用例守住 service 层。
    """

    def setUp(self):
        self.store = Store()
        self.addCleanup(self.store.close)
        self.repository = FakeRepository([
            product("normal_card", "online", "0.04", income=60000),
            student_card_product(),
        ])
        self.service = Agent2Service(self.store, FixtureKnowledgeRead.from_file(),
                                     clock=lambda: NOW, credit_card_repository=self.repository,
                                     llm_components=LlmComponents())
        self.user = self.service.demo_login("student-path-test")["user_id"]
        profile = self.service.create_profile(self.user, synthetic=True)
        self.conversation = self.service.create_conversation(
            self.user, profile.profile_id)["conversation_id"]

    def ask(self, text, key, **kwargs):
        return self.service.post_message(self.user, self.conversation, text, key, **kwargs)

    def test_low_income_student_answers_survive_service_validation(self):
        first = self.ask("我想申请香港信用卡，年收入5万", "s1")
        self.assertEqual(["applicant_age_band"], [q.field for q in first.follow_up_questions])
        second = self.ask("18 岁或以上", "s2",
                          confirmed_fields={"applicant_age_band": "18_or_over"},
                          expected_profile_version=1)
        self.assertEqual(["full_time_student"], [q.field for q in second.follow_up_questions])
        third = self.ask("是全日制学生", "s3",
                         confirmed_fields={"full_time_student": True},
                         expected_profile_version=2)
        self.assertEqual(["student_institution_eligible"],
                         [q.field for q in third.follow_up_questions])
        fourth = self.ask("在指定院校就读", "s4",
                          confirmed_fields={"student_institution_eligible": True},
                          expected_profile_version=3)
        self.assertEqual(["repayment_mode"],
                         [q.field for q in fourth.follow_up_questions])
        fifth = self.ask("每月全额还款", "s5",
                         confirmed_fields={"repayment_mode": "full"},
                         expected_profile_version=4)
        self.assertEqual(["spending_scenarios"], [q.field for q in fifth.follow_up_questions])
        sixth = self.ask("主要网购", "s5b",
                         confirmed_fields={"spending_scenarios": ["online"]},
                         expected_profile_version=5)
        self.assertEqual(["monthly_card_spend_band_hkd"],
                         [q.field for q in sixth.follow_up_questions])
        seventh = self.ask("月消费五千至一万", "s6",
                           confirmed_fields={"monthly_card_spend_band_hkd": "5k_10k"},
                           expected_profile_version=6)
        self.assertEqual(["reward_preference"], [q.field for q in seventh.follow_up_questions])
        final = self.ask("按预计价值比较", "s7",
                         confirmed_fields={"reward_preference": "no_preference"},
                         expected_profile_version=7)
        self.assertEqual("answered", final.status)
        # service 层不暴露 workflow 的 payload，从答案文本断言推荐结果。
        self.assertTrue(final.answer.steps)
        joined = "\n".join(final.answer.steps)
        self.assertIn("主推荐：Student Card", joined)
        self.assertIn("年费：", joined)
        self.assertIn("主要权益：", joined)

    def test_student_with_income_is_not_assumed_income_free(self):
        first = self.ask("我想申请香港信用卡，我是全日制大学生，兼职年收入8万，主要网购", "t1")
        self.assertEqual(["applicant_age_band"], [q.field for q in first.follow_up_questions])
        second = self.ask("18 岁或以上", "t2",
                          confirmed_fields={"applicant_age_band": "18_or_over"},
                          expected_profile_version=1)
        self.assertEqual(["repayment_mode"],
                         [q.field for q in second.follow_up_questions])
        third = self.ask("每月全额还款", "t3",
                         confirmed_fields={"repayment_mode": "full"},
                         expected_profile_version=2)
        self.assertEqual(["monthly_card_spend_band_hkd"],
                         [q.field for q in third.follow_up_questions])
        fourth = self.ask("月消费五千至一万", "t3b",
                          confirmed_fields={"monthly_card_spend_band_hkd": "5k_10k"},
                          expected_profile_version=3)
        self.assertEqual(["reward_preference"],
                         [q.field for q in fourth.follow_up_questions])
        final = self.ask("按预计价值比较", "t4",
                          confirmed_fields={"reward_preference": "no_preference"},
                          expected_profile_version=4)
        self.assertEqual("answered", final.status)
        # 年收入 8 万 ≥ 6 万：普通卡凭收入 eligible 且净值更高——有收入的学生
        # 得到的是普通卡主推，而非被默认成无收入人群只看学生卡。
        joined = "\n".join(final.answer.steps)
        self.assertIn("主推荐：Normal Card", joined)
        # 普通卡路径已经足够，系统不额外收集学生院校资格，也不把学生卡伪装成已符合资格的备选。
        self.assertNotIn("Student Card", joined)


class CreditCardNonResidentServiceIntegrationTests(unittest.TestCase):
    """非香港居民：不再整体拒答，继续推荐并披露获批几率很低（真实 post_message 链路）。"""

    def setUp(self):
        self.store = Store()
        self.addCleanup(self.store.close)
        card = product("resident_card", "online", "0.04")
        card = card.model_copy(update={"eligibility_rules": list(card.eligibility_rules) + [
            EligibilityRule(rule_id="resident", rule_type="residency", operator="eq",
                            value_text="Hong Kong resident"),
        ]})
        self.repository = FakeRepository([card])
        self.service = Agent2Service(self.store, FixtureKnowledgeRead.from_file(),
                                     clock=lambda: NOW, credit_card_repository=self.repository,
                                     llm_components=LlmComponents())
        self.user = self.service.demo_login("non-resident-test")["user_id"]
        profile = self.service.create_profile(self.user, synthetic=True)
        self.conversation = self.service.create_conversation(
            self.user, profile.profile_id)["conversation_id"]

    def ask(self, text, key, **kwargs):
        return self.service.post_message(self.user, self.conversation, text, key, **kwargs)

    def test_non_resident_gets_recommendations_with_low_chance_disclosure(self):
        first = self.ask("我想在香港办一张信用卡，年薪30万，每月全额还款，主要网购", "n1")
        self.assertEqual(["applicant_age_band"], [q.field for q in first.follow_up_questions])
        second = self.ask("18 岁或以上", "n2",
                          confirmed_fields={"applicant_age_band": "18_or_over"},
                          expected_profile_version=1)
        self.assertEqual(["hk_resident"], [q.field for q in second.follow_up_questions])
        third = self.ask("否，我不是香港居民", "n3",
                         confirmed_fields={"hk_resident": False},
                         expected_profile_version=2)
        self.assertEqual(["monthly_card_spend_band_hkd"],
                         [q.field for q in third.follow_up_questions])
        fourth = self.ask("月消费五千至一万", "n4",
                          confirmed_fields={"monthly_card_spend_band_hkd": "5k_10k"},
                          expected_profile_version=3)
        self.assertEqual(["reward_preference"], [q.field for q in fourth.follow_up_questions])
        final = self.ask("按预计价值比较", "n5",
                         confirmed_fields={"reward_preference": "no_preference"},
                         expected_profile_version=4)
        # 关键行为：非居民不再触发 cannot_answer，仍给出推荐。
        self.assertEqual("answered", final.status)
        joined = "\n".join(final.answer.steps)
        self.assertIn("主推荐：Resident Card", joined)
        self.assertIn("年费：", joined)
        self.assertIn("申请限制：", joined)
        self.assertIn("非香港居民", joined)
        self.assertIn("几率很低", joined)
        joined = " ".join(final.answer.limitations)
        self.assertIn("你不是香港居民", joined)
        self.assertIn("最终由银行审批", joined)
        self.assertNotIn("满足：", joined)
        self.assertNotIn("不满足：", joined)


if __name__ == "__main__":
    unittest.main()
