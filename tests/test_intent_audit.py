"""P3: intent confidence, refusal copy, and routing audit trail."""

import asyncio
import unittest
from datetime import datetime, timezone

from agent2.knowledge import FixtureKnowledgeRead
from agent2.llm.factory import LlmComponents
from agent2.llm.schemas import UnderstandingResult
from agent2.orchestration import OrchestrationInput, build_default_workflow
from agent2.orchestration.planner import RuleTaskPlanner
from agent2.runner import SCOPE_BOUNDARY
from agent2.service import Agent2Service
from agent2.store import Store
from tests.test_credit_card_workflow import FakeRepository, product

NOW = datetime(2026, 10, 14, tzinfo=timezone.utc)


def _plan(text, **kwargs):
    request = OrchestrationInput(
        text=text, profile_id="p1", profile_version=1, **kwargs)
    return asyncio.run(RuleTaskPlanner().plan(request))


class RoutingAuditTests(unittest.TestCase):
    def test_rules_hit_records_evidence_and_confidence(self):
        plan = _plan("有什么visa卡推荐")
        audit = plan.routing_audit
        self.assertIsNotNone(audit)
        self.assertEqual(["credit_card"], audit.selected_intents)
        self.assertEqual(1, len(audit.rules_hits))
        self.assertEqual("rules", audit.rules_hits[0].source)
        self.assertGreaterEqual(audit.rules_hits[0].confidence, 0.95)
        self.assertTrue(audit.rules_hits[0].evidence)
        self.assertEqual([], audit.llm_intents)
        self.assertEqual("rules", plan.tasks[0].source)

    def test_llm_recovery_tagged_separately(self):
        plan = _plan("credti card please", llm_intents=["credit_card"],
                     understanding_applied=True)
        audit = plan.routing_audit
        self.assertEqual(["credit_card"], audit.selected_intents)
        self.assertEqual([], audit.rules_hits)
        self.assertEqual(["credit_card"], [hit.intent for hit in audit.llm_intents])
        self.assertEqual("llm", plan.tasks[0].source)
        self.assertAlmostEqual(0.80, plan.tasks[0].confidence)

    def test_out_of_scope_refusal_message(self):
        output = build_default_workflow().run(OrchestrationInput(
            text="帮我选校并审查租房合同", profile_id="p1", profile_version=1))
        self.assertEqual("out_of_scope", output.answer.status)
        self.assertEqual(SCOPE_BOUNDARY, output.answer.answer.summary)
        audit = output.snapshot.task_plan.routing_audit
        self.assertEqual("out_of_scope", audit.refusal_reason)
        self.assertEqual(["out_of_scope"], audit.unsupported_intents)
        self.assertEqual([], audit.selected_intents)
        # Routing audit must not leak into customer-facing notices.
        self.assertFalse(any(n.code == "intent_routing" for n in output.answer.notices))

    def test_other_refusal_suggests_examples(self):
        output = build_default_workflow().run(OrchestrationInput(
            text="今天天气怎么样", profile_id="p1", profile_version=1))
        self.assertEqual("out_of_scope", output.answer.status)
        self.assertIn("推荐信用卡", output.answer.answer.summary)
        self.assertEqual("other", output.snapshot.task_plan.routing_audit.refusal_reason)

    def test_partial_out_of_scope_keeps_business_task(self):
        output = build_default_workflow().run(OrchestrationInput(
            text="帮我选校顺便问问信用卡", profile_id="p1", profile_version=1))
        self.assertIn("credit_card",
                      [task.intent for task in output.snapshot.task_plan.tasks])
        self.assertIn("out_of_scope", output.snapshot.task_plan.unsupported_intents)
        self.assertTrue(any("超出服务范围" in note for note in output.answer.answer.limitations))
        self.assertFalse(any(n.code == "partial_out_of_scope" for n in output.answer.notices))

    def test_llm_only_audit_stays_on_snapshot_not_notices(self):
        output = build_default_workflow(
            credit_card_repository=FakeRepository([product("online_card", "online", "0.04")]),
        ).run(OrchestrationInput(
            text="credti card please", profile_id="p1", profile_version=1,
            profile_values={"application_region": "CN-HK", "applicant_age": 25,
                            "annual_income_hkd": 200000, "repayment_mode": "full",
                            "spending_scenarios": ["online"],
                            "monthly_card_spend_band_hkd": "5k_10k",
                            "reward_preference": "no_preference"},
            llm_intents=["credit_card"], understanding_applied=True,
        ))
        audit = output.snapshot.task_plan.routing_audit
        self.assertEqual(["credit_card"], [h.intent for h in audit.llm_intents])
        self.assertFalse(any(n.code == "intent_routing" for n in output.answer.notices))
        self.assertFalse(any(n.code == "llm_routing_low_confidence" for n in output.answer.notices))
        self.assertTrue(any("换个更明确的说法" in note for note in output.answer.answer.limitations))


class _ContextUnderstanding:
    """Return credit_card only for typo paraphrases; otherwise empty (do not invent intents)."""

    model_version = "understanding-model"
    prompt_version = "understanding-prompt"

    def __init__(self):
        self.calls = []

    async def understand(self, request, allowed_fields):
        self.calls.append(request)
        text = request.text.casefold()
        if "credti" in text or "creadit" in text:
            return UnderstandingResult(intents=["credit_card"])
        return UnderstandingResult(intents=[])


class ServiceAuditRoutingTests(unittest.TestCase):
    def setUp(self):
        self.store = Store()
        self.addCleanup(self.store.close)
        self.understanding = _ContextUnderstanding()
        self.service = Agent2Service(
            self.store, FixtureKnowledgeRead.from_file(), clock=lambda: NOW,
            credit_card_repository=FakeRepository([product("online_card", "online", "0.04")]),
            llm_components=LlmComponents(understanding=self.understanding),
        )
        self.user = self.service.demo_login("audit")["user_id"]
        profile = self.service.create_profile(self.user, synthetic=True)
        self.conversation = self.service.create_conversation(
            self.user, profile.profile_id)["conversation_id"]

    def test_service_refusal_out_of_scope(self):
        answer = self.service.post_message(
            self.user, self.conversation, "帮我申请学校代办", "oos-1")
        self.assertEqual("out_of_scope", answer.status)
        self.assertEqual(SCOPE_BOUNDARY, answer.answer.summary)
        self.assertFalse(any(n.code == "intent_routing" for n in answer.notices))

    def test_service_typo_does_not_expose_routing_json(self):
        answer = self.service.post_message(
            self.user, self.conversation, "credti card for students", "typo-audit")
        self.assertNotEqual("cannot_answer", answer.status)
        self.assertFalse(any(n.code == "intent_routing" for n in answer.notices))
        self.assertFalse(any((n.message or "").strip().startswith("{") for n in answer.notices))

    def test_fx_mode_is_not_a_profile_proposal(self):
        answer = self.service.post_message(
            self.user, self.conversation, "美国交学费怎么汇", "fx-1")
        codes = {p.field_code for p in answer.profile_change_proposals}
        self.assertNotIn("fx_mode", codes)
        self.assertIn("remittance_purpose", codes)


if __name__ == "__main__":
    unittest.main()
