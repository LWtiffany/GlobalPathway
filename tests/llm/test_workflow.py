import unittest

from agent2.llm.schemas import UnderstandingResult
from agent2.models import AnswerBody
from agent2.orchestration import OrchestrationInput, build_default_workflow

from tests.test_credit_card_workflow import FakeRepository, product


PROFILE = {
    "application_region": "CN-HK",
    "applicant_age": 25,
    "annual_income_hkd": 200000,
    "repayment_mode": "full",
    "spending_scenarios": ["online"],
    "monthly_card_spend_band_hkd": "5k_10k",
    "reward_preference": "no_preference",
}


class FakeUnderstanding:
    model_version = "understanding-model"
    prompt_version = "understanding-prompt"

    def __init__(self, result=None, error=None):
        self.result = result or UnderstandingResult(intents=["credit_card"])
        self.error = error
        self.calls = []

    async def understand(self, request, allowed_fields):
        self.calls.append(request)
        if self.error:
            raise self.error
        return self.result


class FakeWriter:
    model_version = "writer-model"
    prompt_version = "writer-prompt"

    def __init__(self, error=None):
        self.error = error
        self.calls = []

    async def write(self, *, request, verified_answer):
        self.calls.append((request, verified_answer))
        if self.error:
            raise self.error
        return AnswerBody(summary="模型润色后的信用卡建议", steps=verified_answer.answer.steps,
                          materials=verified_answer.answer.materials,
                          limitations=verified_answer.answer.limitations)


class LlmWorkflowTests(unittest.TestCase):
    def workflow(self, understanding, writer):
        return build_default_workflow(
            credit_card_repository=FakeRepository([product("online_card", "online", ".04")]),
            understanding=understanding,
            writer=writer,
        )

    def test_free_text_calls_understanding_then_writer(self):
        understanding, writer = FakeUnderstanding(), FakeWriter()
        request = OrchestrationInput(text="帮我选一张合适的卡片", profile_id="p1",
                                     profile_version=1, profile_values=PROFILE)

        output = self.workflow(understanding, writer).run(request)

        self.assertEqual("模型润色后的信用卡建议", output.answer.answer.summary)
        self.assertEqual(1, len(understanding.calls))
        self.assertEqual(1, len(writer.calls))
        self.assertEqual("understanding-model", output.snapshot.model_versions["understanding"])
        self.assertEqual("writer-prompt", output.snapshot.prompt_versions["writer"])

    def test_structured_selection_skips_understanding_but_can_write_answer(self):
        understanding, writer = FakeUnderstanding(), FakeWriter()
        request = OrchestrationInput(
            text="已提交选择", profile_id="p1", profile_version=2, profile_values=PROFILE,
            conversation_intents=["credit_card"], input_mode="structured_selection")

        output = self.workflow(understanding, writer).run(request)

        self.assertEqual([], understanding.calls)
        self.assertEqual(1, len(writer.calls))
        self.assertEqual("模型润色后的信用卡建议", output.answer.answer.summary)

    def test_model_failure_returns_deterministic_answer(self):
        understanding = FakeUnderstanding(error=TimeoutError("understanding timeout"))
        writer = FakeWriter(error=TimeoutError("writer timeout"))
        request = OrchestrationInput(text="推荐信用卡", profile_id="p1", profile_version=1,
                                     profile_values=PROFILE)

        output = self.workflow(understanding, writer).run(request)

        self.assertIn("主推荐", output.answer.answer.summary)
        self.assertIn("llm_fallback", {notice.code for notice in output.answer.notices})
