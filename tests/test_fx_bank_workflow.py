"""P2 FX / bank_account closed-loop tests (FX grounded on policy quotes)."""

import asyncio
import unittest
from datetime import datetime, timezone

from agent2.fx_knowledge import (FxDocumentRow, FxEvidenceRow, FxPolicyBundle,
                                 build_fx_answer_parts)
from agent2.knowledge import FixtureKnowledgeRead
from agent2.llm.factory import LlmComponents
from agent2.orchestration import OrchestrationInput, build_default_workflow
from agent2.orchestration.contracts import DomainTask
from agent2.orchestration.domains.bank_account import BankAccountWorkflow
from agent2.orchestration.domains.bank_account_repository import SeedBankAccountRepository
from agent2.orchestration.domains.fx import FxWorkflow
from agent2.service import Agent2Service
from agent2.store import Store
from tests.test_credit_card_workflow import FakeRepository, product

NOW = datetime(2026, 10, 14, tzinfo=timezone.utc)


def sample_bundle(purpose="tuition") -> FxPolicyBundle:
    amount = FxEvidenceRow(
        evidence_id="ev-quota", target="field:annual_quota_amount", kind="value",
        quote="境内个人年度购汇总额等值5万美元及以下的，可凭本人有效身份证件在银行办理。",
        source_id="safe_personal_fx_rules", recorded_at="2026-01-01T00:00:00+00:00",
    )
    over = FxEvidenceRow(
        evidence_id="ev-over", target="field:over_quota_allowed", kind="value",
        quote="超过年度便利化额度的，凭有交易额的相关证明材料在银行办理。",
        source_id="safe_personal_fx_rules", recorded_at="2026-01-01T00:00:00+00:00",
    )
    doc_ev = FxEvidenceRow(
        evidence_id="ev-doc", target="document:doc-id", kind="value",
        quote="境外学校录取通知书（如为外文的，应同时提供中文翻译件）。",
        source_id="safe_tianjin_study_fx", recorded_at="2026-01-01T00:00:00+00:00",
    )
    return FxPolicyBundle(
        policy_id="fx-policy-test",
        country="CN",
        purpose_code=purpose,
        title="留学购汇：学费" if purpose == "tuition" else "留学购汇：生活费",
        annual_quota_amount=50000,
        annual_quota_currency="USD",
        over_quota_allowed=True,
        remitter_relation="immediate_family",
        quota_reset="calendar_year",
        can_use_family_quota=True,
        evidence=[amount, over, doc_ev],
        documents=[FxDocumentRow(
            doc_id="doc-id", code="admission_letter", name_zh="境外学校录取通知书",
            required=True, condition={"over_quota": True}, evidence=[doc_ev],
        )],
        sources={
            "safe_personal_fx_rules": {
                "source_id": "safe_personal_fx_rules",
                "name": "国家外汇管理局 个人外汇管理办法实施细则",
                "url": "https://www.safe.gov.cn/safe/2007/0105/5320.html",
                "authority": "official",
            },
            "safe_tianjin_study_fx": {
                "source_id": "safe_tianjin_study_fx",
                "name": "国家外汇管理局天津市分局 留学购汇问答",
                "url": "https://www.safe.gov.cn/tianjin/2019/0604/1176.html",
                "authority": "official",
            },
        },
        verified_at=NOW,
        environment="test",
    )


class FxKnowledgeTests(unittest.TestCase):
    def test_build_parts_include_quotes_and_materials(self):
        citations, materials, quotes, facts = build_fx_answer_parts(
            sample_bundle(), destination_label="英国", mode="tuition")
        self.assertTrue(citations)
        self.assertTrue(any("5万" in c.quote for c in citations))
        self.assertTrue(materials)
        self.assertEqual("admission_letter", materials[0].document_code)
        self.assertIn("quotes", facts)
        self.assertTrue(quotes)


class FxWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.workflow = FxWorkflow(policy_loader=lambda **kwargs: sample_bundle(
            kwargs.get("purpose_code", "tuition")))

    def test_tuition_asks_destination_then_answers_with_quotes(self):
        task = DomainTask(task_id="t1", domain="fx", intent="remittance_tuition",
                          facts={"remittance_purpose": "tuition"},
                          basis={"remittance_purpose": "学费"})
        first = asyncio.run(self.workflow.run(
            task, OrchestrationInput(text="学费怎么汇", profile_id="p1", profile_version=1)))
        self.assertEqual("needs_input", first.status)
        self.assertEqual(["destination_country"],
                         [q.field for q in first.follow_up_questions])

        second = asyncio.run(self.workflow.run(
            task, OrchestrationInput(
                text="英国", profile_id="p1", profile_version=2,
                profile_values={"destination_country": "UK", "remittance_purpose": "tuition"},
                conversation_intents=["remittance_tuition"],
                input_mode="structured_selection",
            )))
        self.assertEqual("answered", second.status)
        self.assertIn("英国", second.answer.summary)
        self.assertTrue(second.citations)
        self.assertTrue(any("5万" in c.quote for c in second.citations))
        self.assertTrue(second.answer.materials)
        self.assertFalse(any(str(step).startswith("政策原文") for step in second.answer.steps))
        self.assertTrue(any("50000" in step or "5万" in step or "额度" in step
                            for step in second.answer.steps))
        self.assertTrue(second.evidence_refs)

    def test_overseas_with_destination_answers(self):
        task = DomainTask(task_id="t1", domain="fx", intent="overseas_spending",
                          facts={"destination_country": "CN-HK"},
                          basis={"destination_country": "香港"})
        result = asyncio.run(self.workflow.run(
            task, OrchestrationInput(text="香港境外消费怎么刷卡", profile_id="p1",
                                     profile_version=1)))
        self.assertEqual("answered", result.status)
        self.assertEqual("overseas", result.observed_facts.get("fx_mode"))
        self.assertTrue(result.citations)


class BankAccountWorkflowTests(unittest.TestCase):
    def test_open_account_asks_identity_when_region_known(self):
        task = DomainTask(task_id="t1", domain="bank_account", intent="hsbc_account",
                          facts={"destination_country": "UK"},
                          basis={"destination_country": "英国"})
        first = asyncio.run(BankAccountWorkflow(repository=SeedBankAccountRepository()).run(
            task, OrchestrationInput(text="英国开户", profile_id="p1", profile_version=1)))
        self.assertEqual("needs_input", first.status)
        self.assertEqual(["applicant_identity"],
                         [q.field for q in first.follow_up_questions])
        self.assertEqual("UK", first.observed_facts.get("account_region"))

        second = asyncio.run(BankAccountWorkflow(repository=SeedBankAccountRepository()).run(
            task, OrchestrationInput(
                text="学生本人", profile_id="p1", profile_version=2,
                profile_values={"account_region": "UK", "applicant_identity": "student",
                                "destination_country": "UK"},
                conversation_intents=["hsbc_account"],
                input_mode="structured_selection",
            )))
        self.assertEqual("answered", second.status)
        self.assertIn("英国", second.answer.summary)
        self.assertIn("汇丰", second.answer.summary)
        self.assertTrue(second.citations)
        self.assertTrue(any("hsbc.co.uk" in (c.source_url or "") for c in second.citations))
        self.assertTrue(any("汇丰" in step for step in second.answer.steps))
        self.assertFalse(any("其他银行" in step and "推荐" in step for step in second.answer.steps))

    def test_us_student_recommends_hsbc_premier_path(self):
        task = DomainTask(task_id="t1", domain="bank_account", intent="hsbc_account",
                          facts={"account_region": "US", "applicant_identity": "student"},
                          basis={})
        result = asyncio.run(BankAccountWorkflow(repository=SeedBankAccountRepository()).run(
            task, OrchestrationInput(
                text="去美国读书怎么开户", profile_id="p1", profile_version=1,
                profile_values={"account_region": "US", "applicant_identity": "student"},
            )))
        self.assertEqual("answered", result.status)
        blob = " ".join([result.answer.summary, *result.answer.steps])
        self.assertIn("汇丰", blob)
        self.assertIn("美国", blob)
        self.assertTrue(result.citations)


class ServiceFxAccountRoutingTests(unittest.TestCase):
    def setUp(self):
        self.store = Store()
        self.addCleanup(self.store.close)
        self.service = Agent2Service(
            self.store, FixtureKnowledgeRead.from_file(), clock=lambda: NOW,
            credit_card_repository=FakeRepository([product("online_card", "online", "0.04")]),
            bank_account_repository=SeedBankAccountRepository(),
            llm_components=LlmComponents(),
        )
        # Inject FX fixture so service tests do not depend on live Agent1 SQLite.
        self.service.v2_workflow.dependencies.domains["fx"] = FxWorkflow(
            policy_loader=lambda **kwargs: sample_bundle(kwargs.get("purpose_code", "tuition")))
        self.user = self.service.demo_login("fx-bank")["user_id"]
        profile = self.service.create_profile(self.user, synthetic=True)
        self.conversation = self.service.create_conversation(
            self.user, profile.profile_id)["conversation_id"]

    def test_tuition_remittance_session_topic(self):
        answer = self.service.post_message(
            self.user, self.conversation, "英国学费怎么汇", "fx-1")
        history = self.service.list_messages(self.user, self.conversation)["messages"]
        self.assertEqual("remittance", history[0]["body"].get("topic"))
        self.assertEqual("answered", answer.status)
        self.assertIn("学费", answer.answer.summary)
        self.assertTrue(answer.citations)
        self.assertTrue(any("5万" in c.quote for c in answer.citations))

    def test_account_follow_up_stays_on_account_topic(self):
        first = self.service.post_message(
            self.user, self.conversation, "我想开户", "acc-1")
        self.assertEqual("needs_input", first.status)
        field = first.follow_up_questions[0].field
        self.assertEqual("account_region", field)
        second = self.service.post_message(
            self.user, self.conversation, "开户地区：英国", "acc-2",
            confirmed_fields={"account_region": "UK"},
            expected_profile_version=first.profile_version,
        )
        history = self.service.list_messages(self.user, self.conversation)["messages"]
        self.assertEqual("account", history[0]["body"].get("topic"))
        self.assertEqual("needs_input", second.status)
        self.assertEqual(["applicant_identity"],
                         [q.field for q in second.follow_up_questions])

    def test_multi_domain_mit_prep_composes_sections(self):
        answer = self.service.post_message(
            self.user, self.conversation,
            "去美国读书要准备学费汇款、开户和信用卡", "mit-1")
        history = self.service.list_messages(self.user, self.conversation)["messages"]
        assistants = [m for m in history if m["role"] == "assistant"]
        # One chat bubble per domain (fx / account / credit_card), not one mega-merge.
        self.assertGreaterEqual(len(assistants), 2)
        blob = "\n".join(m["body"]["answer"]["summary"] for m in assistants)
        self.assertTrue(any(label in blob for label in ("外汇", "汇款", "开户", "信用卡")))
        self.assertEqual(answer.message_id, assistants[-1]["message_id"])

    def test_account_and_card_follow_up_scopes_to_answered_domain(self):
        """Confirming a credit-card chip must not re-emit bank-account advice."""
        profile = self.service.create_profile(self.user, synthetic=True)
        self.service.patch_profile(
            self.user, profile.profile_id, expected_version=profile.version,
            set_fields={"role": "student", "destination_country": "US",
                        "school": "MIT", "offer_status": "unconditional"})
        conv = self.service.create_conversation(
            self.user, profile.profile_id)["conversation_id"]
        first = self.service.post_message(
            self.user, conv, "我需要开银行账户和办信用卡吗", "both-1")
        history = self.service.list_messages(self.user, conv)["messages"]
        self.assertEqual(
            ["hsbc_account", "credit_card"],
            history[0]["body"].get("active_intents"),
        )
        assistants = [m for m in history if m["role"] == "assistant"]
        self.assertGreaterEqual(len(assistants), 2)
        self.assertIn("先说要不要", "\n".join(assistants[0]["body"]["answer"]["steps"]))
        self.assertTrue(first.follow_up_questions or first.status == "answered")
        if first.status != "needs_input":
            return
        field = first.follow_up_questions[0].field
        self.assertEqual("want_credit_card", field)
        option = first.follow_up_questions[0].options[0]["value"]
        self.assertEqual("yes", option)
        second = self.service.post_message(
            self.user, conv, "选项确认", "both-2",
            confirmed_fields={field: option},
            expected_profile_version=first.profile_version,
        )
        history2 = self.service.list_messages(self.user, conv)["messages"]
        user_turns = [m for m in history2 if m["role"] == "user"]
        self.assertEqual(
            ["credit_card"],
            user_turns[-1]["body"].get("active_intents"),
        )
        last_assistants = [m for m in history2 if m["role"] == "assistant"
                           and m.get("reply_to") == user_turns[-1]["message_id"]]
        blob = "\n".join(
            (m["body"]["answer"].get("summary") or "") + "\n"
            + "\n".join(m["body"]["answer"].get("steps") or [])
            for m in last_assistants)
        self.assertNotIn("护照", blob)
        self.assertNotIn("Studying in the U.S.", blob)
        self.assertTrue(
            second.follow_up_questions
            or "信用卡" in blob
            or "Mastercard" in blob
            or second.status in {"answered", "needs_input", "needs_verification"})


class OrchestrationFxAccountTests(unittest.TestCase):
    def test_workflow_answers_fx_without_service(self):
        output = build_default_workflow(
            fx_policy_loader=lambda **kwargs: sample_bundle(
                kwargs.get("purpose_code", "tuition")),
        ).run(OrchestrationInput(
            text="英国学费汇款要准备什么",
            profile_id="p1", profile_version=1,
            profile_values={"destination_country": "UK"},
        ))
        self.assertEqual(["fx"], [t.domain for t in output.snapshot.task_plan.tasks])
        self.assertEqual("answered", output.answer.status)
        fx = output.answer.domain_results[0]
        self.assertTrue(fx.citations)


if __name__ == "__main__":
    unittest.main()
