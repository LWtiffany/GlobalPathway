"""Application-side checks for model output before it enters business state."""

import re
from collections.abc import Callable

from agent2.models import AnswerBody
from agent2.orchestration.contracts import OrchestrationInput

from .schemas import UnderstandingResult

MIN_FACT_CONFIDENCE = 0.80
NUMBER_PATTERN = re.compile(r"(?<![\w])\d+(?:\.\d+)?%?")


def validate_understanding(result: UnderstandingResult, request: OrchestrationInput,
                           allowed_fields: set[str], value_validator: Callable[[str, object], str | None],
                           *, min_confidence: float = MIN_FACT_CONFIDENCE):
    facts, basis = {}, {}
    text_folded = request.text.casefold()
    for item in result.facts:
        if item.field_code not in allowed_fields or item.confidence < min_confidence:
            continue
        evidence = item.evidence.strip()
        if not evidence or evidence.casefold() not in text_folded:
            continue
        if value_validator(item.field_code, item.value):
            continue
        if item.field_code not in facts or item.confidence > facts[item.field_code][1]:
            facts[item.field_code] = (item.value, item.confidence)
            basis[item.field_code] = evidence
    return ({code: value for code, (value, _) in facts.items()}, basis,
            list(dict.fromkeys(result.intents)))


def validate_written_answer(candidate: AnswerBody, source: AnswerBody) -> None:
    """Reject new numeric claims and keep locked materials/limitations byte-for-byte."""
    source_text = " ".join([source.summary, *source.steps, *source.limitations])
    candidate_text = " ".join([candidate.summary, *candidate.steps])
    unsupported_numbers = set(NUMBER_PATTERN.findall(candidate_text)) - set(NUMBER_PATTERN.findall(source_text))
    if unsupported_numbers:
        raise ValueError("writer introduced unsupported numeric facts: " + ", ".join(sorted(unsupported_numbers)))
    if candidate.materials != source.materials or candidate.limitations != source.limitations:
        raise ValueError("writer changed locked answer fields")
