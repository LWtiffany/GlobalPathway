import asyncio
import unittest

from pydantic import ValidationError

from agent2.models import AnswerBody
from agent2.orchestration import OrchestrationInput, build_default_workflow
from agent2.orchestration.contracts import DomainResult
from agent2.orchestration.planner import RuleTaskPlanner
from agent2.orchestration.quality import DefaultQualityGate


class Agent2OrchestrationTests(unittest.TestCase):
    def request(self, text):
        return OrchestrationInput(text=text, profile_id="profile-1", profile_version=3,
                                  profile_values={"destination_country": "UK"})

    def test_single_intent_uses_fast_path(self):
        output = build_default_workflow().run(self.request("英国学生签证需要什么材料？"))

        self.assertEqual(["visa"], [item.domain for item in output.snapshot.task_plan.tasks])
        self.assertEqual("cannot_answer", output.answer.status)
        self.assertNotEqual("已按领域拆分并汇总。", output.answer.answer.summary)

    def test_multi_intent_is_split_and_composed(self):
        output = build_default_workflow().run(
            self.request("英国学生签证、学费汇款、银行开户和信用卡分别要准备什么？")
        )

        self.assertEqual(
            ["visa", "fx", "bank_account", "credit_card"],
            [item.domain for item in output.snapshot.task_plan.tasks],
        )
        self.assertEqual("已按领域拆分并汇总。", output.answer.answer.summary)
        self.assertEqual(4, len(output.answer.domain_results))
        self.assertEqual(["cannot_answer", "cannot_answer", "cannot_answer", "needs_input"],
                         [item.status for item in output.answer.domain_results])

    def test_credit_card_account_phrase_is_one_domain(self):
        output = build_default_workflow().run(self.request("留学生信用卡开户需要什么？"))

        self.assertEqual(["credit_card"],
                         [item.domain for item in output.snapshot.task_plan.tasks])

    def test_structured_credit_card_choices_cannot_create_fx_intent(self):
        request = OrchestrationInput(
            text="每月全额还款；网购；境外消费；内地消费",
            profile_id="profile-1",
            profile_version=3,
            conversation_intents=["credit_card"],
            asked_fields=["repayment_mode", "spending_scenarios"],
            input_mode="structured_selection",
        )

        plan = asyncio.run(RuleTaskPlanner().plan(request))

        self.assertEqual(["credit_card"], [task.domain for task in plan.tasks])
        self.assertEqual(["credit_card"], [task.intent for task in plan.tasks])

    def test_profile_and_plan_changes_are_candidates_only(self):
        output = build_default_workflow().run(self.request("25岁，想汇学费"))

        changes = {item.field_code: item.new_value
                   for item in output.lifecycle.profile_change_candidates}
        self.assertEqual(25, changes["applicant_age"])
        self.assertEqual("tuition", changes["remittance_purpose"])
        self.assertTrue(output.lifecycle.plan_item_candidates)

    def test_answer_snapshot_is_frozen(self):
        output = build_default_workflow().run(self.request("我想开户"))

        with self.assertRaises(ValidationError):
            output.snapshot.profile_version = 4

    def test_gate_downgrades_unsupported_answer_without_evidence(self):
        result = DomainResult(task_id="task-1", domain="visa", status="answered",
                              answer=AnswerBody(summary="确定结论"))
        report = asyncio.run(DefaultQualityGate().check([result]))

        self.assertEqual("needs_verification", report.results[0].status)
        self.assertIn("task-1: answered result has no evidence", report.findings)


if __name__ == "__main__":
    unittest.main()
