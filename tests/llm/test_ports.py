import asyncio
import unittest

from agent2.llm.answer_writer import LlmAnswerWriter
from agent2.llm.schemas import FactCandidate, UnderstandingResult, WrittenAnswer
from agent2.llm.understanding import LlmUserUnderstanding
from agent2.models import AnswerBody
from agent2.orchestration.contracts import OrchestrationInput, UnifiedAnswer

from .fakes import ScriptedStructuredModel


class LlmPortTests(unittest.TestCase):
    def test_understanding_renders_context_and_uses_schema(self):
        expected = UnderstandingResult(
            intents=["credit_card"],
            facts=[FactCandidate(field_code="full_time_student", value=True,
                                 confidence=.95, evidence="全日制学生")],
        )
        model = ScriptedStructuredModel(expected)
        port = LlmUserUnderstanding(model)
        request = OrchestrationInput(text="我是全日制学生", profile_id="p1", profile_version=1,
                                     profile_values={"application_region": "CN-HK"})

        actual = asyncio.run(port.understand(request, {"full_time_student": {"kind": "bool"}}))

        self.assertEqual(expected, actual)
        self.assertEqual("understand_user", model.calls[0]["operation"])
        self.assertNotIn("CN-HK", model.calls[0]["prompt"])
        model.assert_complete()

    def test_writer_preserves_locked_answer_fields(self):
        model = ScriptedStructuredModel(WrittenAnswer(
            summary="A 卡最适合你的网购场景。", steps=["建议先核对申请材料。"]))
        port = LlmAnswerWriter(model)
        request = OrchestrationInput(text="推荐信用卡", profile_id="p1", profile_version=1)
        verified = UnifiedAnswer(
            status="answered",
            answer=AnswerBody(
                summary="主推荐 A 卡",
                steps=["主推荐：A 卡；资格：可提交资产证明，需银行个案审批。"],
                limitations=["需审批"],
            ),
        )

        actual = asyncio.run(port.write(request=request, verified_answer=verified))

        self.assertEqual(["需审批"], actual.limitations)
        self.assertEqual(
            "主推荐：A 卡；资格：可提交资产证明，需银行个案审批。", actual.steps[0])
        self.assertNotIn("建议先核对申请材料。", actual.steps)
        self.assertEqual("write_answer", model.calls[0]["operation"])
        model.assert_complete()
