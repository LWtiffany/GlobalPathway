"""Customer-service answers grounded in the reviewed HSBC account seed."""

import asyncio
import unittest

from agent2.llm.account_subintent import AccountFact, AccountSubintentResult
from agent2.llm.schemas import WrittenAnswer
from agent2.orchestration.contracts import DomainTask, OrchestrationInput
from agent2.orchestration.domains.bank_account import BankAccountWorkflow
from agent2.orchestration.domains.bank_account_repository import SeedBankAccountRepository
from agent2.orchestration.workflow import build_default_workflow
from tests.llm.fakes import ScriptedStructuredModel

TASK = DomainTask(task_id="account-1", domain="bank_account", intent="hsbc_account")


def ask(text, *, profile=None, workflow=None):
    request = OrchestrationInput(text=text, profile_id="p1", profile_version=1,
                                 profile_values=profile or {})
    return asyncio.run((workflow or BankAccountWorkflow(
        repository=SeedBankAccountRepository())).run(TASK, request))


class EnglishUsRepository:
    """Mimic the shared RDS rows whose descriptive fields are stored in English."""

    async def read(self, market):
        catalog = await SeedBankAccountRepository().read(market)
        if market == "US":
            catalog["products"][0]["summary"] = (
                "HSBC USA's principal personal checking relationship with qualification routes.")
            catalog["routes"][0]["route_name"] = "US Premier online application"
            catalog["routes"][0]["route_note"] = (
                "Joint applications or cases outside online criteria may require a callback.")
            descriptions = [
                "Proof of permanent address",
                "Passport or other accepted government identity document",
                "Proof of enrollment",
            ]
            for row, description in zip(catalog["documents"], descriptions):
                row["description"] = description
        return catalog


class BankAccountCustomerServiceTests(unittest.TestCase):
    def test_opening_process_localizes_english_rds_rows(self):
        result = ask("我要怎么开户", profile={"destination_country": "US", "role": "student"},
                     workflow=BankAccountWorkflow(repository=EnglishUsRepository()))
        blob = " ".join([result.answer.summary, *result.answer.steps])
        language_check = blob.replace("HSBC Premier Checking", "")
        self.assertEqual(["documents_process"], result.payload["subintents"])
        self.assertIn("可以按下面的方式准备", result.answer.summary)
        self.assertIn("申请方式", blob)
        self.assertIn("身份证明", blob)
        self.assertIn("地址证明", blob)
        self.assertNotRegex(language_check, r"\b(?:[A-Za-z]{2,}\s+){2,}[A-Za-z]{2,}\b")
        self.assertNotIn("Proof of", blob)
        self.assertNotIn("Joint applications", blob)

    def test_writer_english_process_answer_is_rejected(self):
        model = ScriptedStructuredModel(
            AccountSubintentResult(
                primary_subintent="documents_process", subintents=["documents_process"], facts=[]),
            WrittenAnswer(
                summary="About HSBC Premier Checking application.",
                steps=["Proof of permanent address and passport are required."]),
        )
        result = ask("我要怎么开户", profile={"destination_country": "US", "role": "student"},
                     workflow=BankAccountWorkflow(model=model, repository=EnglishUsRepository()))
        blob = " ".join([result.answer.summary, *result.answer.steps])
        self.assertEqual("ValueError", result.payload["model_failure"])
        self.assertIn("申请方式", blob)
        self.assertNotIn("Proof of", blob)

    def test_broad_us_product_question_discloses_incomplete_catalog(self):
        result = ask("我要开什么户", profile={"destination_country": "US", "role": "student"})
        blob = " ".join([result.answer.summary, *result.answer.steps])
        self.assertEqual(["product_selection"], result.payload["subintents"])
        self.assertEqual("US", result.payload["account_region"])
        self.assertEqual(1, result.payload["catalog_product_count"])
        self.assertEqual("limited", result.payload["selection_coverage"])
        self.assertIn("不足", blob)
        self.assertIn("不能据此判断", blob)
        self.assertNotIn("还缺少决定资格的条件", blob)
        self.assertNotIn("Applicant must", blob)
        self.assertIn("申请人是否已满 18 岁", blob)
        self.assertEqual("applicant_age", result.follow_up_questions[0].field)

    def test_writer_cannot_recommend_from_incomplete_catalog(self):
        model = ScriptedStructuredModel(
            AccountSubintentResult(
                primary_subintent="product_selection", subintents=["product_selection"], facts=[]),
            WrittenAnswer(summary="建议你开 HSBC Premier Checking。",
                          steps=["这是最适合你的账户。"]),
        )
        result = ask("我要开什么户", profile={"destination_country": "US", "role": "student"},
                     workflow=BankAccountWorkflow(
                         model=model, repository=SeedBankAccountRepository()))
        blob = " ".join([result.answer.summary, *result.answer.steps])
        self.assertEqual("ValueError", result.payload["model_failure"])
        self.assertIn("不足", blob)
        self.assertNotIn("建议你开", blob)

    def test_uk_new_arrival_student_condition_and_alternative(self):
        result = ask("我刚到英国，没住满三年，能开 Student Bank Account 吗？")
        self.assertEqual("answered", result.status)
        candidates = result.payload["candidate_results"]
        self.assertEqual("hsbc_uk_student", candidates[0]["product"]["account_id"])
        self.assertEqual("does_not_meet_published_criteria", candidates[0]["status"])
        self.assertEqual("hsbc_uk_bank", candidates[1]["product"]["account_id"])
        self.assertIn("三年", " ".join(result.answer.steps).replace("3", "三"))
        self.assertTrue(result.citations)

    def test_hk_parent_question_uses_child_age(self):
        result = ask("我是家长，孩子17岁去香港，能开储蓄账户吗？",
                     profile={"role": "parent"})
        self.assertEqual("student", result.observed_facts["applicant_identity"])
        self.assertEqual(17, result.observed_facts["applicant_age"])
        self.assertEqual("hsbc_hk_children_savings",
                         result.payload["candidate_results"][0]["product"]["account_id"])
        self.assertEqual("meets_published_criteria",
                         result.payload["candidate_results"][0]["status"])
        self.assertEqual("needs_input",
                         result.payload["candidate_results"][0]["routes"][0]["status"])

    def test_hk_one_balance_is_not_opening_minimum(self):
        result = ask("香港 HSBC One 开户最低要存一万吗？", profile={"role": "student"})
        blob = " ".join(result.answer.steps)
        self.assertIn("低结余费", blob)
        self.assertIn("开户最低存款", blob)
        self.assertIn("hsbc_hk_one", result.evidence_refs)
        self.assertTrue(any("hsbc.com.hk" in citation.source_url for citation in result.citations))

    def test_hk_one_and_premier_comparison_keeps_both_named_products(self):
        result = ask("香港 HSBC One 和 Premier 有什么不同？",
                     profile={"role": "student"})
        ids = [item["product"]["account_id"]
               for item in result.payload["candidate_results"]]
        blob = " ".join([result.answer.summary, *result.answer.steps])
        self.assertEqual("compare_products", result.payload["dialogue_act"])
        self.assertEqual(["hsbc_hk_one", "hsbc_hk_premier"], ids)
        self.assertIn("HSBC One", blob)
        self.assertIn("HSBC Premier", blob)
        self.assertIn("1,000,000", blob)
        self.assertIn("香港身份证持有人无低结余费", blob)
        self.assertIn("非香港身份证客户于 2026 年 1 月 1 日或之后开户", blob)
        self.assertIn("怎么选", blob)

    def test_hkid_holder_does_not_get_non_hkid_balance_condition(self):
        result = ask("我有香港身份证，HSBC One 会收低结余费吗？")
        blob = " ".join(result.answer.steps)
        self.assertIn("不适用", blob)
        self.assertNotIn("另有公开的免月费路径", blob)

    def test_condition_correction_recalculates(self):
        profile = {"role": "parent", "applicant_age": 17, "account_region": "CN-HK"}
        old = ask("孩子17岁去香港能开账户吗", profile=profile)
        new = ask("刚才说错了，孩子已经18岁，香港能开账户吗", profile=profile)
        self.assertEqual(17, old.observed_facts["applicant_age"])
        self.assertEqual(18, new.observed_facts["applicant_age"])
        self.assertEqual("hsbc_hk_one",
                         new.payload["candidate_results"][0]["product"]["account_id"])

    def test_model_subintent_and_writer_are_in_customer_service_path(self):
        model = ScriptedStructuredModel(
            AccountSubintentResult(
                primary_subintent="fees_balance", subintents=["fees_balance"],
                facts=[AccountFact(field_code="account_region", value="CN-HK", evidence="香港"),
                       AccountFact(field_code="account_product", value="HSBC One", evidence="HSBC One")],
            ),
            WrittenAnswer(summary="香港 HSBC One 的费用要看身份证和结余条件。",
                          steps=["请按官网核对低结余费与豁免条件。"]),
        )
        result = ask("香港 HSBC One 有月费吗", workflow=BankAccountWorkflow(
            model=model, repository=SeedBankAccountRepository()))
        self.assertEqual(["account_subintent", "write_bank_answer"],
                         [call["operation"] for call in model.calls])
        self.assertEqual(["fees_balance"], result.payload["subintents"])
        self.assertEqual("香港 HSBC One 的费用要看身份证和结余条件。", result.answer.summary)
        self.assertTrue(result.citations)

    def test_writer_negated_fee_reassurance_is_accepted(self):
        # "无月费" closes the fee topic instead of expanding into it; the real
        # writer (qwen-plus) reliably phrases Children Savings reassurance this
        # way and rejecting it degraded the answer to the rules fallback.
        model = ScriptedStructuredModel(
            AccountSubintentResult(
                primary_subintent="eligibility", subintents=["eligibility"], facts=[]),
            WrittenAnswer(summary="孩子符合年龄条件，而且账户无月费、无最低结余要求。",
                          steps=["可前往网点办理。"]),
        )
        result = ask("孩子17岁在香港能开户吗", workflow=BankAccountWorkflow(
            model=model, repository=SeedBankAccountRepository()))
        self.assertIn("无月费", result.answer.summary)
        self.assertIsNone(result.payload["model_failure"])

    def test_writer_cannot_expand_into_unasked_fees(self):
        model = ScriptedStructuredModel(
            AccountSubintentResult(
                primary_subintent="eligibility", subintents=["eligibility"], facts=[]),
            WrittenAnswer(summary="孩子符合年龄条件，不过结余低于10,000港元时每月收100港元低结余费。",
                          steps=["可前往网点办理。"]),
        )
        result = ask("孩子17岁在香港能开户吗", workflow=BankAccountWorkflow(
            model=model, repository=SeedBankAccountRepository()))
        self.assertNotIn("低结余费", result.answer.summary)
        self.assertEqual("ValueError", result.payload["model_failure"])
        self.assertIn("bank_llm_fallback", [notice.code for notice in result.notices])

    def test_top_level_customer_service_routes_to_grounded_account_answer(self):
        workflow = build_default_workflow(bank_repository=SeedBankAccountRepository())
        output = workflow.run(OrchestrationInput(
            text="汇丰香港 HSBC One 每月有管理费吗", profile_id="p1", profile_version=1))
        self.assertEqual("answered", output.answer.status)
        result = output.answer.domain_results[0]
        self.assertEqual("bank_account", result.domain)
        self.assertEqual(["fees_balance"], result.payload["subintents"])
        self.assertIn("低结余费", " ".join(result.answer.steps))
        self.assertTrue(result.citations)


if __name__ == "__main__":
    unittest.main()
