import unittest

from agent2.llm.schemas import FactCandidate, UnderstandingResult
from agent2.llm.validation import validate_understanding, validate_written_answer
from agent2.models import AnswerBody
from agent2.orchestration.contracts import OrchestrationInput


class ValidationTests(unittest.TestCase):
    def request(self):
        return OrchestrationInput(text="我是全日制学生，主要网购", profile_id="p1", profile_version=1)

    def test_understanding_requires_whitelist_confidence_evidence_and_valid_value(self):
        result = UnderstandingResult(intents=["credit_card"], facts=[
            FactCandidate(field_code="full_time_student", value=True, confidence=.95, evidence="全日制学生"),
            FactCandidate(field_code="repayment_mode", value="full", confidence=.99, evidence="不存在"),
            FactCandidate(field_code="secret_field", value="x", confidence=.99, evidence="网购"),
            FactCandidate(field_code="spending_scenarios", value=["invalid"], confidence=.99, evidence="网购"),
            FactCandidate(field_code="spending_scenarios", value=["online"], confidence=.5, evidence="网购"),
        ])
        validator = lambda code, value: None if (code, value) == ("full_time_student", True) else "bad"

        facts, basis, intents = validate_understanding(
            result, self.request(), {"full_time_student", "repayment_mode", "spending_scenarios"}, validator)

        self.assertEqual({"full_time_student": True}, facts)
        self.assertEqual({"full_time_student": "全日制学生"}, basis)
        self.assertEqual(["credit_card"], intents)

    def test_writer_cannot_add_numeric_claim(self):
        source = AnswerBody(summary="主推荐 A 卡", steps=["APR 35.42%"], limitations=["需审批"])
        candidate = AnswerBody(summary="主推荐 A 卡，可获 10% 回赠", limitations=["需审批"])
        with self.assertRaisesRegex(ValueError, "unsupported numeric"):
            validate_written_answer(candidate, source)

    def test_writer_cannot_change_locked_fields(self):
        source = AnswerBody(summary="A", limitations=["需审批"])
        candidate = AnswerBody(summary="A", limitations=[])
        with self.assertRaisesRegex(ValueError, "locked"):
            validate_written_answer(candidate, source)
