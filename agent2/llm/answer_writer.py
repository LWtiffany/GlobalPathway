"""Natural-language writer for already verified orchestration results."""

import json

from agent2.models import AnswerBody
from agent2.orchestration.contracts import OrchestrationInput, UnifiedAnswer

from .contracts import StructuredModel
from .prompts import PromptRenderer
from .schemas import WrittenAnswer

PROMPT_VERSION = "write-answer-v1"


class LlmAnswerWriter:
    prompt_version = PROMPT_VERSION

    def __init__(self, model: StructuredModel, renderer: PromptRenderer | None = None):
        self.model = model
        self.model_version = model.model_name
        self.renderer = renderer or PromptRenderer()

    async def write(self, *, request: OrchestrationInput,
                    verified_answer: UnifiedAnswer) -> AnswerBody:
        prompt = self.renderer.render(
            "write_answer.jinja2",
            user_text=request.text,
            verified_json=json.dumps({
                "status": verified_answer.status,
                "answer": verified_answer.answer.model_dump(mode="json", exclude={"materials"}),
                "domains": [{
                    "domain": item.domain,
                    "status": item.status,
                    "answer": item.answer.model_dump(mode="json", exclude={"materials"}),
                    "payload": item.payload,
                    "next_actions": item.next_actions,
                } for item in verified_answer.domain_results],
            }, ensure_ascii=False, sort_keys=True, default=str),
        )
        written = await self.model.parse(prompt=prompt, output_type=WrittenAnswer,
                                         operation="write_answer")
        # Eligibility, fees and borrowing costs are deterministic decision facts. Keep them
        # verbatim; the model may add a clearer explanation but cannot erase these details.
        steps = list(verified_answer.answer.steps)
        steps.extend(step for step in written.steps if step not in steps)
        candidate = AnswerBody(
            summary=written.summary,
            steps=steps,
            materials=verified_answer.answer.materials,
            limitations=verified_answer.answer.limitations,
        )
        return candidate
