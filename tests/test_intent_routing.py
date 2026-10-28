"""P0 intent routing gold cases: paraphrases, multi-intent, and disambiguation."""

import asyncio
import unittest
from datetime import datetime, timezone

from agent2.intent import classify, find_intent_matches
from agent2.knowledge import FixtureKnowledgeRead
from agent2.llm.factory import LlmComponents
from agent2.orchestration.contracts import OrchestrationInput
from agent2.orchestration.planner import INTENT_DOMAIN, RuleTaskPlanner
from agent2.service import Agent2Service
from agent2.store import Store
from tests.test_credit_card_workflow import FakeRepository, product


def _plan(text, *, conversation_intents=(), input_mode="free_text", asked_fields=()):
    request = OrchestrationInput(
        text=text,
        profile_id="profile-1",
        profile_version=1,
        conversation_intents=list(conversation_intents),
        asked_fields=list(asked_fields),
        input_mode=input_mode,
    )
    return asyncio.run(RuleTaskPlanner().plan(request))


def _intents(text, **kwargs):
    return [task.intent for task in _plan(text, **kwargs).tasks]


def _domains(text, **kwargs):
    return [task.domain for task in _plan(text, **kwargs).tasks]


class CreditCardParaphraseTests(unittest.TestCase):
    """单域 paraphrase → 必须路由到 credit_card。"""

    CASES = [
        "我想办信用卡",
        "有什么信用卡优惠",
        "有啥信用卡优惠",
        "Visa卡推荐",
        "有什么visa卡推荐",
        "推荐一张 Mastercard",
        "万事达卡哪张好",
        "威士卡有什么好处",
        "留学生信用卡怎么选",
        "留学生卡推荐一下",
        "帮我看看刷卡优惠",
        "开卡优惠有哪些",
        "办卡需要什么条件",
        "申卡难度大吗",
        "有没有联名卡",
        "白金卡和世界之极差在哪",
        "哪张卡适合境外用",
        "什么卡年费低",
        "积分里程怎么选卡",
        "credit card for students",
        "想办一张卡出国用",
        "汇丰有什么卡推荐",
    ]

    def test_paraphrases_route_to_credit_card(self):
        for text in self.CASES:
            with self.subTest(text=text):
                self.assertIn("credit_card", _intents(text), msg=text)
                self.assertIn("credit_card", _domains(text), msg=text)
                self.assertEqual("credit_card", classify(text).intent, msg=text)


class MultiIntentTests(unittest.TestCase):
    def test_tuition_and_card(self):
        self.assertEqual(
            ["remittance_tuition", "credit_card"],
            _intents("学费汇款和信用卡分别要准备什么"),
        )

    def test_account_and_card(self):
        self.assertEqual(
            ["hsbc_account", "credit_card"],
            _intents("英国开户和办信用卡怎么准备"),
        )

    def test_visa_tuition_account_card(self):
        self.assertEqual(
            ["visa", "fx", "bank_account", "credit_card"],
            _domains("英国学生签证、学费汇款、银行开户和信用卡分别要准备什么？"),
        )

    def test_mit_prep_multi_domain(self):
        domains = _domains("去MIT读书行前要准备签证学费汇款开户和信用卡吗")
        self.assertIn("visa", domains)
        self.assertIn("fx", domains)
        self.assertIn("bank_account", domains)
        self.assertIn("credit_card", domains)


class DisambiguationTests(unittest.TestCase):
    def test_credit_card_account_is_not_bank_account(self):
        self.assertEqual(["credit_card"], _domains("留学生信用卡开户需要什么？"))
        self.assertEqual(["credit_card"], _domains("信用卡开卡流程"))

    def test_bare_account_is_bank_account(self):
        self.assertEqual(["bank_account"], _domains("我想开户"))

    def test_overseas_spend_not_credit_card(self):
        intents = _intents("我想了解在香港境外消费怎么刷卡")
        self.assertEqual(["overseas_spending"], intents)
        self.assertNotIn("credit_card", intents)

    def test_card_benefit_not_fx(self):
        self.assertEqual(["credit_card"], _intents("帮我看看刷卡优惠"))
        self.assertNotIn("overseas_spending", _intents("帮我看看刷卡优惠"))

    def test_student_visa_not_visa_card(self):
        self.assertEqual(["visa_documents"], _intents("英国学生签证材料"))
        self.assertNotIn("credit_card", _intents("英国学生签证材料"))

    def test_out_of_scope_school_choice(self):
        plan = _plan("帮我选校顺便问问信用卡")
        self.assertIn("out_of_scope", plan.unsupported_intents)
        self.assertIn("credit_card", [task.intent for task in plan.tasks])

    def test_structured_choice_labels_stay_on_credit_card(self):
        intents = _intents(
            "每月全额还款；网购；境外消费；内地消费",
            conversation_intents=["credit_card"],
            asked_fields=["repayment_mode", "spending_scenarios"],
            input_mode="structured_selection",
        )
        self.assertEqual(["credit_card"], intents)


class FxAccountParaphraseTests(unittest.TestCase):
    def test_fx_and_remittance_paraphrases(self):
        cases = [
            ("tuition", "remittance_tuition", ["交学费怎么汇", "tuition fee payment", "学费汇款"]),
            ("living", "remittance_living", ["生活费怎么转", "living expenses", "生活开支汇款"]),
            ("fx", "overseas_spending", ["换汇要怎么做", "购汇额度", "外汇和汇率", "foreign exchange"]),
        ]
        for label, intent, texts in cases:
            for text in texts:
                with self.subTest(label=label, text=text):
                    self.assertIn(intent, _intents(text), msg=text)
                    self.assertEqual("fx", INTENT_DOMAIN[intent])

    def test_account_paraphrases(self):
        for text in ("我想开户", "汇丰账户怎么开", "open a bank account", "英国学生账户"):
            with self.subTest(text=text):
                self.assertIn("hsbc_account", _intents(text))
                self.assertEqual(["bank_account"], _domains(text))

    def test_account_does_not_steal_credit_card_open(self):
        self.assertEqual(["credit_card"], _domains("留学生信用卡开户需要什么？"))

    def test_bare_remittance_is_living_not_tuition(self):
        self.assertEqual(["remittance_living"], _intents("怎么汇款"))
        self.assertNotIn("remittance_tuition", _intents("怎么汇款"))


class IntentMatchHelperTests(unittest.TestCase):
    def test_find_intent_matches_preserves_positions(self):
        matches = find_intent_matches("学费和信用卡")
        by_intent = {intent: start for start, intent in matches}
        self.assertLess(by_intent["remittance_tuition"], by_intent["credit_card"])

    def test_intent_domain_map_covers_supported(self):
        for intent in ("visa_documents", "remittance_tuition", "remittance_living",
                       "overseas_spending", "hsbc_account", "credit_card"):
            self.assertIn(intent, INTENT_DOMAIN)


class ServiceTaskPlanRoutingTests(unittest.TestCase):
    """Service 入口按 TaskPlan 进信用卡，不依赖 classify 单标签恰好是 credit_card。"""

    def setUp(self):
        self.store = Store()
        self.addCleanup(self.store.close)
        self.service = Agent2Service(
            self.store, FixtureKnowledgeRead.from_file(),
            clock=lambda: datetime(2026, 10, 14, tzinfo=timezone.utc),
            credit_card_repository=FakeRepository([product("online_card", "online", "0.04")]),
            llm_components=LlmComponents(),
        )
        self.user = self.service.demo_login("intent-route")["user_id"]
        profile = self.service.create_profile(self.user, synthetic=True)
        self.conversation = self.service.create_conversation(
            self.user, profile.profile_id)["conversation_id"]

    def test_visa_card_paraphrase_enters_credit_card_module(self):
        answer = self.service.post_message(
            self.user, self.conversation, "有什么visa卡推荐", "visa-card-1")
        self.assertNotEqual("cannot_answer", answer.status)
        self.assertIn(answer.status, {"needs_input", "answered", "needs_verification"})
        # 会话落盘 topic 应为信用卡
        history = self.service.list_messages(self.user, self.conversation)["messages"]
        user_body = history[0]["body"]
        self.assertEqual("credit_card", user_body.get("topic"))

    def test_tuition_first_classify_still_routes_card_via_plan(self):
        # classify 单标签会先命中学费；TaskPlan 含信用卡时仍进 v2。
        self.assertEqual("remittance_tuition", classify("学费汇款和信用卡怎么准备").intent)
        answer = self.service.post_message(
            self.user, self.conversation, "学费汇款和信用卡怎么准备", "multi-1")
        history = self.service.list_messages(self.user, self.conversation)["messages"]
        self.assertEqual("credit_card", history[0]["body"].get("topic"))
        self.assertNotEqual("cannot_answer", answer.status)


class ServiceLlmUnionRoutingTests(unittest.TestCase):
    """P1: 规则未命中时，LLM understanding 并集仍能路由进信用卡；追问轮不二次理解。"""

    def setUp(self):
        from agent2.llm.schemas import UnderstandingResult
        from tests.llm.test_workflow import FakeUnderstanding

        self.store = Store()
        self.addCleanup(self.store.close)
        self.understanding = FakeUnderstanding(UnderstandingResult(intents=["credit_card"]))
        self.service = Agent2Service(
            self.store, FixtureKnowledgeRead.from_file(),
            clock=lambda: datetime(2026, 10, 14, tzinfo=timezone.utc),
            credit_card_repository=FakeRepository([product("online_card", "online", "0.04")]),
            llm_components=LlmComponents(understanding=self.understanding),
        )
        self.user = self.service.demo_login("intent-llm")["user_id"]
        profile = self.service.create_profile(self.user, synthetic=True)
        self.conversation = self.service.create_conversation(
            self.user, profile.profile_id)["conversation_id"]

    def test_typo_paraphrase_recovered_by_llm_routes_to_credit_card(self):
        text = "credti card for students"
        self.assertNotIn("credit_card", [i for _, i in find_intent_matches(text)])
        answer = self.service.post_message(self.user, self.conversation, text, "typo-1")
        self.assertEqual(1, len(self.understanding.calls))
        history = self.service.list_messages(self.user, self.conversation)["messages"]
        self.assertEqual("credit_card", history[0]["body"].get("topic"))
        self.assertNotEqual("cannot_answer", answer.status)

    def test_rule_miss_card_phrasing_recovered_by_llm(self):
        text = "帮我选一张合适的卡片"
        self.assertNotIn("credit_card", [i for _, i in find_intent_matches(text)])
        self.service.post_message(self.user, self.conversation, text, "para-1")
        history = self.service.list_messages(self.user, self.conversation)["messages"]
        self.assertEqual("credit_card", history[0]["body"].get("topic"))

    def test_structured_follow_up_does_not_call_understanding(self):
        # First turn enters credit card via rules.
        first = self.service.post_message(
            self.user, self.conversation, "推荐信用卡", "cc-1")
        self.assertTrue(first.follow_up_questions)
        calls_after_first = len(self.understanding.calls)
        field = first.follow_up_questions[0].field
        option = first.follow_up_questions[0].options[0]["value"]
        self.service.post_message(
            self.user, self.conversation, "选项确认", "cc-2",
            confirmed_fields={field: option},
            expected_profile_version=first.profile_version,
        )
        # Pending asked+topic path must not re-run understanding on option labels.
        self.assertEqual(calls_after_first, len(self.understanding.calls))


if __name__ == "__main__":
    unittest.main()
