import asyncio
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from agent2.orchestration.contracts import DomainTask, OrchestrationInput
from agent2.orchestration.domains.credit_card import CreditCardWorkflow
from agent2.orchestration.domains.credit_card_engine import recommend
from agent2.orchestration.domains.credit_card_models import (
    CardCatalogRead, CardFee, CreditCardProduct, EligibilityRule, RewardRule,
)
from agent2.knowledge import FixtureKnowledgeRead
from agent2.service import Agent2Service
from agent2.store import Store

NOW = datetime(2026, 10, 18, tzinfo=timezone.utc)


def product(card_id, category, rate, *, status="verified", income=100000, network="Visa"):
    return CreditCardProduct(
        card_id=card_id, issuing_bank="Test Bank", card_network=network, market="Hong Kong",
        product_name=card_id.replace("_", " ").title(), data_status=status,
        official_url=f"https://example.com/{card_id}" if status == "verified" else None,
        last_verified_at=NOW if status == "verified" else None,
        eligibility_rules=[
            EligibilityRule(rule_id=f"{card_id}-age", rule_type="minimum_age", operator="gte",
                            value_numeric=Decimal("18")),
            EligibilityRule(rule_id=f"{card_id}-income", rule_type="minimum_income", operator="gte",
                            value_numeric=Decimal(str(income)), currency="HKD"),
        ],
        reward_rules=[
            RewardRule(rule_id=f"{card_id}-base", spend_category="general", reward_type="cashback",
                       reward_rate=Decimal("0.004")),
            RewardRule(rule_id=f"{card_id}-special", spend_category=category, reward_type="cashback",
                       reward_rate=Decimal(str(rate))),
        ],
        fees=[CardFee(fee_id=f"{card_id}-annual", fee_type="annual_fee", amount=Decimal("0"),
                      currency="HKD", waiver_condition="No annual fee")],
    )


class FakeRepository:
    def __init__(self, products):
        self.products = products
        self.called = False

    async def active_products(self, market, now):
        self.called = True
        return CardCatalogRead(read_id="read-1", market=market, products=self.products)


def request(**values):
    defaults = {"application_region": "CN-HK", "applicant_age": 25, "annual_income_hkd": 200000,
                "repayment_mode": "full", "spending_scenarios": "online,travel"}
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
        self.assertEqual(["application_region", "applicant_age_band"],
                         [question.field for question in result.follow_up_questions])
        self.assertTrue(all(q.input_type == "single_select" for q in result.follow_up_questions))
        self.assertTrue(all(q.persist_mode == "save_on_select" for q in result.follow_up_questions))
        self.assertFalse(repo.called)

    def test_carrying_balance_stops_reward_ranking_when_apr_is_missing(self):
        repo = FakeRepository([product("online_card", "online", "0.04")])
        result = asyncio.run(CreditCardWorkflow(repo, clock=lambda: NOW).run(
            TASK, request(repayment_mode="sometimes_carry")))

        self.assertEqual("needs_verification", result.status)
        self.assertIn("APR", result.answer.summary)
        self.assertFalse(repo.called)

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

    def test_synthetic_rds_data_is_never_a_definite_recommendation(self):
        repo = FakeRepository([product("demo_card", "online", "0.04", status="synthetic")])
        result = asyncio.run(CreditCardWorkflow(repo, clock=lambda: NOW).run(TASK, request()))

        self.assertEqual("needs_verification", result.status)
        self.assertIn("【模拟推荐】", result.answer.limitations[0])

    def test_no_fee_alternatives_require_eligibility_and_are_capped(self):
        cards = [product(f"card_{index}", "online", f"0.0{index + 1}", income=100000)
                 for index in range(6)]
        cards.append(product("too_expensive", "online", "0.05", income=999999))
        result = recommend(cards, {"applicant_age": 25, "annual_income_hkd": 200000}, ["online"])

        self.assertLessEqual(len(result.alternatives), 3)
        self.assertIn("too_expensive", result.excluded)


class CreditCardServiceIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.store = Store()
        self.addCleanup(self.store.close)
        self.repository = FakeRepository([product("online_card", "online", "0.04")])
        self.service = Agent2Service(self.store, FixtureKnowledgeRead.from_file(),
                                     clock=lambda: NOW, credit_card_repository=self.repository)
        self.user = self.service.demo_login("credit-card-test")["user_id"]
        profile = self.service.create_profile(self.user, synthetic=True)
        self.conversation = self.service.create_conversation(self.user, profile.profile_id)["conversation_id"]

    def ask(self, text, key):
        return self.service.post_message(self.user, self.conversation, text, key)

    def test_existing_messages_endpoint_runs_the_credit_card_workflow(self):
        first = self.ask("想申请香港信用卡", "m1")
        self.assertEqual("needs_input", first.status)
        self.assertEqual(["applicant_age_band", "annual_income_band_hkd"],
                         [item.field for item in first.follow_up_questions])
        income = first.follow_up_questions[1]
        self.assertTrue(income.options)
        self.assertEqual("single_select", income.input_type)

        second = self.ask("25岁，年收入20万港币", "m2")
        self.assertEqual("needs_input", second.status)
        self.assertEqual(["repayment_mode", "spending_scenarios"],
                         [item.field for item in second.follow_up_questions])

        third = self.ask("每月全额还款，主要网购", "m3")
        self.assertEqual("answered", third.status)
        self.assertIn("Online Card", third.answer.summary)
        self.assertTrue(self.repository.called)
        profile_id = self.service._owned_conversation(self.user, self.conversation)["profile_id"]
        advice = self.service.get_advice(self.user, profile_id, "credit_card")
        self.assertEqual("online_card", advice["payload"]["recommendations"]["primary"]["card_id"])
        proposal_fields = {item.field_code for item in self.service.list_proposals(self.user, profile_id)}
        self.assertEqual({"application_region", "applicant_age", "annual_income_hkd",
                          "repayment_mode", "spending_scenarios"}, proposal_fields)
        run = self.store.one("SELECT prompt_version, gate_version FROM answer_runs WHERE message_id=?",
                             (third.message_id,))
        self.assertEqual((run["prompt_version"], run["gate_version"]),
                         ("credit-card-template-1", "orchestration-gate-1"))

    def test_structured_follow_up_choices_are_saved_to_profile_atomically(self):
        first = self.ask("想申请信用卡", "choice-1")
        self.assertEqual(["application_region", "applicant_age_band"],
                         [q.field for q in first.follow_up_questions])
        second = self.service.post_message(
            self.user, self.conversation, "申请地区：中国香港；年龄区间：18 岁或以上", "choice-2",
            confirmed_fields={"application_region": "CN-HK", "applicant_age_band": "18_or_over"},
            expected_profile_version=1)
        self.assertEqual(["annual_income_band_hkd"], [q.field for q in second.follow_up_questions])
        income_value = second.follow_up_questions[0].options[-1]["value"]
        third = self.service.post_message(
            self.user, self.conversation, "税前年收入区间：最高档", "choice-3",
            confirmed_fields={"annual_income_band_hkd": income_value}, expected_profile_version=2)
        self.assertEqual(["repayment_mode", "spending_scenarios"],
                         [q.field for q in third.follow_up_questions])
        final = self.service.post_message(
            self.user, self.conversation, "每月全额还款；主要消费场景：网购", "choice-4",
            confirmed_fields={"repayment_mode": "full", "spending_scenarios": ["online"]},
            expected_profile_version=3)
        self.assertEqual("answered", final.status)
        profile_id = self.service._owned_conversation(self.user, self.conversation)["profile_id"]
        profile = self.service.get_profile(self.user, profile_id)
        self.assertEqual(4, profile.version)
        for code in ("application_region", "applicant_age_band", "annual_income_band_hkd",
                     "repayment_mode", "spending_scenarios"):
            self.assertEqual("user_selection", profile.fields[code].source_type)
            self.assertEqual("confirmed", profile.fields[code].status)
        events = self.store.all("SELECT field_code FROM profile_field_events WHERE profile_id=?",
                                (profile_id,))
        self.assertEqual(5, len(events))
        self.assertEqual([], self.service.list_proposals(self.user, profile_id))


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


if __name__ == "__main__":
    unittest.main()
