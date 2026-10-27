import asyncio
import unittest

from pydantic import ValidationError

from agent2.models import AnswerBody, FollowUpQuestion
from agent2.orchestration import OrchestrationInput, build_default_workflow
from agent2.orchestration.composer import DefaultResponseComposer
from agent2.orchestration.contracts import DomainResult, QualityReport, TaskPlan
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
        self.assertTrue(output.answer.answer.summary.endswith("我分开说。"))
        self.assertIn("银行开户", output.answer.answer.summary)
        self.assertIn("信用卡", output.answer.answer.summary)
        self.assertEqual(4, len(output.answer.domain_results))
        self.assertEqual("cannot_answer", output.answer.domain_results[0].status)  # visa TODO
        self.assertIn(output.answer.domain_results[1].status, {"answered", "needs_input"})  # fx
        self.assertIn(output.answer.domain_results[2].status, {"answered", "needs_input"})  # account
        # needs_input when catalog is available; needs_verification if product repo is down.
        self.assertIn(output.answer.domain_results[3].status,
                      {"needs_input", "needs_verification"})
        self.assertEqual("credit_card", output.answer.domain_results[3].domain)
        steps = output.answer.answer.steps
        self.assertIn("## 签证", steps)
        self.assertIn("## 外汇与汇款", steps)
        self.assertIn("## 银行开户", steps)
        self.assertIn("## 信用卡", steps)

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
        # fx domain only projects remittance-related profile fields; age stays session-side.
        self.assertEqual("tuition", changes["remittance_purpose"])
        self.assertNotIn("applicant_age", changes)
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

    def test_multi_intent_composer_sections_and_follow_up_quota(self):
        plan = TaskPlan(tasks=[], unsupported_intents=[], planner_version="test")
        results = [
            DomainResult(
                task_id="t1", domain="fx", status="needs_input",
                answer=AnswerBody(summary="还需要汇款用途"),
                follow_up_questions=[FollowUpQuestion(
                    field="remittance_purpose", text="汇款用途？", question_code="ask_purpose",
                    rule="test", profile_version=1,
                    options=[{"value": "tuition", "label": "学费"}])],
            ),
            DomainResult(
                task_id="t2", domain="credit_card", status="needs_input",
                answer=AnswerBody(summary="还需要申请地区", steps=["请选择申请地区"]),
                follow_up_questions=[
                    FollowUpQuestion(
                        field="application_region", text="申请地区？", question_code="ask_region",
                        rule="test", profile_version=1,
                        options=[{"value": "CN-HK", "label": "香港"}]),
                    FollowUpQuestion(
                        field="applicant_age_band", text="年龄？", question_code="ask_age",
                        rule="test", profile_version=1,
                        options=[{"value": "18_or_over", "label": "18+"}]),
                ],
            ),
            DomainResult(
                task_id="t3", domain="bank_account", status="cannot_answer",
                answer=AnswerBody(summary="开户流程尚未接入"),
                follow_up_questions=[FollowUpQuestion(
                    field="destination_country", text="目的地？", question_code="ask_dest",
                    rule="test", profile_version=1,
                    options=[{"value": "UK", "label": "英国"}])],
            ),
        ]
        report = QualityReport(results=results, findings=[], gate_version="test")
        answer = asyncio.run(DefaultResponseComposer().compose(plan, report))

        self.assertEqual("needs_input", answer.status)
        self.assertEqual(["## 外汇与汇款", "还需要汇款用途",
                          "## 信用卡", "还需要申请地区", "请选择申请地区",
                          "## 银行开户", "开户流程尚未接入"], answer.answer.steps)
        # One follow-up per domain, hard cap 2 across domains.
        self.assertEqual(["remittance_purpose", "application_region"],
                         [q.field for q in answer.follow_up_questions])

    def test_multi_intent_composer_keeps_full_domain_steps(self):
        """Multi-intent must not truncate each domain to a short preview."""
        from agent2.models import Material
        plan = TaskPlan(tasks=[], unsupported_intents=[], planner_version="test")
        card_steps = [
            "提前了解：先比较年费与权益。",
            "优先：汇丰美国发行的卡",
            "汇丰美国两款信用卡均要求已有美国汇丰卓越理财支票账户。",
            "HSBC Premier World Mastercard（万事达卡）",
            "年费：95 美元",
            "主要权益：旅行权益",
            "备用：汇丰香港发行的卡（获批可能较难）",
            "香港卡仅作备用参考，不与当地卡混排比较。",
            "Hk Red（威士卡）",
            "年费：免年费",
        ]
        results = [
            DomainResult(
                task_id="t1", domain="bank_account", status="answered",
                answer=AnswerBody(
                    summary="建议优先了解汇丰美国卓越理财支票账户。",
                    steps=[
                        "开户地区：美国；申请人：学生本人。",
                        "优先了解：汇丰美国卓越理财支票账户。",
                        "公开资格要点：年满 18 岁。",
                        "按官网指引在抵达前后申请。",
                    ],
                    materials=[Material(
                        requirement_key="passport", document_code="passport",
                        name_zh="护照", name_en="Passport", applicability="required",
                        citation_id="c1")],
                ),
                evidence_refs=["hsbc-us"],
            ),
            DomainResult(
                task_id="t2", domain="credit_card", status="answered",
                answer=AnswerBody(
                    summary="先介绍美国卡，另附香港卡作备用。",
                    steps=card_steps,
                ),
                evidence_refs=["hsbc-card"],
            ),
        ]
        report = QualityReport(results=results, findings=[], gate_version="test")
        answer = asyncio.run(DefaultResponseComposer().compose(plan, report))
        steps = answer.answer.steps
        self.assertIn("## 银行开户", steps)
        self.assertIn("按官网指引在抵达前后申请。", steps)
        self.assertTrue(any(s.startswith("护照（必需）") for s in steps))
        self.assertIn("## 信用卡", steps)
        for line in card_steps:
            self.assertIn(line, steps)
        self.assertGreater(len(steps), 3 + len(card_steps))

    def test_necessity_question_answers_need_before_advice(self):
        from agent2.orchestration.composer import is_necessity_question
        self.assertTrue(is_necessity_question("我需要开银行账户和办信用卡吗"))
        self.assertTrue(is_necessity_question("要不要办信用卡"))
        self.assertFalse(is_necessity_question("英国学生签证需要什么材料？"))
        self.assertFalse(is_necessity_question("开户和信用卡分别要准备什么"))

        plan = TaskPlan(tasks=[], unsupported_intents=[], planner_version="test")
        results = [
            DomainResult(
                task_id="t1", domain="bank_account", status="answered",
                answer=AnswerBody(summary="建议开汇丰美国账户。", steps=["开户流程一步"]),
                evidence_refs=["a"],
            ),
            DomainResult(
                task_id="t2", domain="credit_card", status="answered",
                answer=AnswerBody(summary="可先了解美国卡。", steps=["年费：95 美元"]),
                evidence_refs=["b"],
            ),
        ]
        report = QualityReport(results=results, findings=[], gate_version="test")
        answer = asyncio.run(DefaultResponseComposer().compose(
            plan, report, user_text="我需要开银行账户和办信用卡吗"))
        self.assertIn("开户一般需要", answer.answer.summary)
        self.assertIn("信用卡不是硬性必需", answer.answer.summary)
        self.assertIn("若你想办卡", answer.answer.summary)
        self.assertEqual("## 先说要不要", answer.answer.steps[0])
        self.assertTrue(any(s.startswith("银行开户：需要") for s in answer.answer.steps))
        self.assertTrue(any(s.startswith("信用卡：不是硬性必需") for s in answer.answer.steps))
        self.assertIn("## 银行开户", answer.answer.steps)
        self.assertIn("开户流程一步", answer.answer.steps)
        self.assertIn("## 信用卡", answer.answer.steps)
        self.assertIn("年费：95 美元", answer.answer.steps)


if __name__ == "__main__":
    unittest.main()
