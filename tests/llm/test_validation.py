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

        facts, basis, intents, topic_switch = validate_understanding(
            result, self.request(), {"full_time_student", "repayment_mode", "spending_scenarios"}, validator)

        self.assertEqual({"full_time_student": True}, facts)
        self.assertEqual({"full_time_student": "全日制学生"}, basis)
        self.assertEqual(["credit_card"], intents)
        self.assertFalse(topic_switch)

    def test_topic_switch_verdict_passes_through(self):
        result = UnderstandingResult(intents=["remittance_living"], topic_switch=True)
        _, _, intents, topic_switch = validate_understanding(
            result, self.request(), set(), lambda code, value: "bad")
        self.assertEqual(["remittance_living"], intents)
        self.assertTrue(topic_switch)

    def test_multi_value_add_is_merged_against_server_profile(self):
        request = OrchestrationInput(
            text="以后也会在内地刷",
            profile_id="p1",
            profile_version=1,
            profile_values={"spending_scenarios": ["online"]},
        )
        result = UnderstandingResult(facts=[FactCandidate(
            field_code="spending_scenarios",
            value=["mainland_china"],
            confidence=.95,
            evidence="也会在内地刷",
            update_mode="add",
        )])

        facts, _, _, _ = validate_understanding(
            result, request, {"spending_scenarios"},
            lambda _code, value: None if value == ["online", "mainland_china"] else "bad",
        )

        self.assertEqual(["online", "mainland_china"], facts["spending_scenarios"])

    def test_multi_value_replace_does_not_carry_old_values(self):
        request = OrchestrationInput(
            text="以后主要在内地刷",
            profile_id="p1",
            profile_version=1,
            profile_values={"spending_scenarios": ["online"]},
        )
        result = UnderstandingResult(facts=[FactCandidate(
            field_code="spending_scenarios",
            value=["mainland_china"],
            confidence=.95,
            evidence="主要在内地刷",
        )])

        facts, _, _, _ = validate_understanding(
            result, request, {"spending_scenarios"}, lambda _code, _value: None)

        self.assertEqual(["mainland_china"], facts["spending_scenarios"])

    def test_writer_cannot_add_numeric_claim(self):
        source = AnswerBody(summary="主推荐 A 卡", steps=["APR 35.42%"], limitations=["需审批"])
        candidate = AnswerBody(summary="主推荐 A 卡，可获 10% 回赠", limitations=["需审批"])
        with self.assertRaisesRegex(ValueError, "unsupported numeric"):
            validate_written_answer(candidate, source)

    def test_writer_cannot_add_numeric_claim_after_chinese_text(self):
        source = AnswerBody(summary="儿童账户开户资料", steps=["申请人未满18岁"], limitations=["需审批"])
        candidate = AnswerBody(summary="儿童账户开户资料", steps=["月费为0港元"], limitations=["需审批"])
        with self.assertRaisesRegex(ValueError, "unsupported numeric"):
            validate_written_answer(candidate, source)

    def test_writer_may_restate_numbers_from_fed_data(self):
        # Rule rows given to the writer carry age bands and thresholds the
        # deterministic answer may spell differently; restating them is grounded.
        source = AnswerBody(summary="儿童账户开户资料", steps=["已提供条件符合收录的公开要求"],
                            limitations=["需审批"])
        candidate = AnswerBody(summary="17岁子女符合年龄条件",
                               steps=["11至17岁的申请人必须由家长陪同到场"], limitations=["需审批"])
        validate_written_answer(
            candidate, source,
            supported_text='{"rules": [{"min_age": 11, "max_age": 17}, {"age": 18}]}')

    def test_writer_may_translate_english_number_words_from_fed_data(self):
        # "valid for at least six months" in rule text is the same fact as "至少6个月".
        source = AnswerBody(summary="开户证件要求", steps=["请核对证件有效期要求"], limitations=["需审批"])
        candidate = AnswerBody(summary="证件剩余有效期至少6个月",
                               steps=["入境后90天内可到港激活账户"], limitations=["需审批"])
        validate_written_answer(
            candidate, source,
            supported_text='{"rules": [{"note": "valid for at least six months"}, '
                           '{"note": "activate within ninety days of arrival"}]}')

    def test_writer_still_cannot_invent_numbers_with_english_words(self):
        source = AnswerBody(summary="开户证件要求", steps=["请核对证件有效期要求"], limitations=["需审批"])
        candidate = AnswerBody(summary="证件剩余有效期至少6个月",
                               steps=["月费为300港元"], limitations=["需审批"])
        with self.assertRaisesRegex(ValueError, "unsupported numeric"):
            validate_written_answer(
                candidate, source,
                supported_text='{"rules": [{"note": "valid for at least six months"}]}')

    def test_writer_still_cannot_invent_numbers_with_supported_text(self):
        source = AnswerBody(summary="儿童账户开户资料", steps=["已提供条件符合收录的公开要求"],
                            limitations=["需审批"])
        candidate = AnswerBody(summary="17岁子女符合年龄条件",
                               steps=["月费为300港元"], limitations=["需审批"])
        with self.assertRaisesRegex(ValueError, "unsupported numeric"):
            validate_written_answer(
                candidate, source,
                supported_text='{"rules": [{"min_age": 11, "max_age": 17}, {"age": 18}]}')

    def test_writer_cannot_change_locked_fields(self):
        source = AnswerBody(summary="A", limitations=["需审批"])
        candidate = AnswerBody(summary="A", limitations=[])
        with self.assertRaisesRegex(ValueError, "locked"):
            validate_written_answer(candidate, source)
