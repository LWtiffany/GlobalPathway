"""Natural-language writer for already verified orchestration results."""

import json
import re

from agent2.models import AnswerBody
from agent2.orchestration.contracts import OrchestrationInput, UnifiedAnswer

from .contracts import StructuredModel
from .prompts import PromptRenderer
from .schemas import CreditCardAdvisorDraft, WrittenAnswer
from .validation import validate_card_advisor

PROMPT_VERSION = "write-answer-v2"


class LlmAnswerWriter:
    prompt_version = PROMPT_VERSION

    def __init__(self, model: StructuredModel, renderer: PromptRenderer | None = None):
        self.model = model
        self.model_version = model.model_name
        self.renderer = renderer or PromptRenderer()

    async def write(self, *, request: OrchestrationInput,
                    verified_answer: UnifiedAnswer) -> AnswerBody:
        card_results = [item for item in verified_answer.domain_results
                        if item.domain == "credit_card"]
        if len(card_results) == 1 and len(verified_answer.domain_results) == 1 \
                and card_results[0].payload.get("capability") in {"product_qa", "general_knowledge"}:
            # A grounded direct answer is already complete. Rewriting it adds
            # latency and can turn a short fee answer into another sales pitch.
            return verified_answer.answer
        if len(card_results) == 1 and len(verified_answer.domain_results) == 1 \
                and card_results[0].payload.get("capability") != "general_knowledge" \
                and verified_answer.answer.steps:
            source = verified_answer.answer
            lines = {f"s{index}": step for index, step in enumerate(source.steps, 1)}
            prompt = self.renderer.render(
                "credit_card_advisor.jinja2", user_text=request.text,
                lines_json=json.dumps(lines, ensure_ascii=False),
                limitations_json=json.dumps(source.limitations, ensure_ascii=False))
            draft = await self.model.parse(prompt=prompt, output_type=CreditCardAdvisorDraft,
                                           operation="credit_card_advisor")
            validate_card_advisor(draft, lines, source)
            analysis = re.sub(r"\s*[（(]\s*s\d+(?:\s*[,，、;；]\s*s\d+)*\s*[）)]",
                              "", draft.analysis, flags=re.I).strip()
            if re.search(r"(?<![A-Za-z0-9])s\d+(?![A-Za-z0-9])", analysis, re.I):
                raise ValueError("advisor exposed an internal evidence ID")
            return AnswerBody(summary=source.summary,
                              steps=[analysis, *source.steps],
                              materials=source.materials, limitations=source.limitations)
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
        # FX / bank_account already draft grounded copy with domain prompts.
        grounded_domains = {
            item.domain for item in verified_answer.domain_results
            if item.domain in {"fx", "bank_account"} and item.citations
        }
        present = {item.domain for item in verified_answer.domain_results}
        if grounded_domains and present <= grounded_domains:
            return verified_answer.answer

        written = await self.model.parse(prompt=prompt, output_type=WrittenAnswer,
                                         operation="write_answer")
        # Eligibility, fees and borrowing costs are deterministic decision facts.
        # The LLM may refine the summary, but appending near-duplicate prose to
        # these locked steps produced repeated card recommendations in real runs.
        steps = list(verified_answer.answer.steps)
        # Credit-card output contains regulated product terms. A model summary
        # can sound fluent while adding an unmodelled promotion or condition,
        # so both its heading and decision steps remain deterministic.
        credit_card_answer = any(item.domain == "credit_card"
                                 for item in verified_answer.domain_results)
        candidate = AnswerBody(
            summary=(verified_answer.answer.summary if credit_card_answer else written.summary),
            steps=steps,
            materials=verified_answer.answer.materials,
            limitations=verified_answer.answer.limitations,
        )
        return candidate
