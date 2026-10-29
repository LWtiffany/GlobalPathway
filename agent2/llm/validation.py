"""Application-side checks for model output before it enters business state."""

import re
from collections.abc import Callable
from typing import NamedTuple

from agent2.models import AnswerBody
from agent2.orchestration.contracts import OrchestrationInput

from .schemas import UnderstandingResult


class UnderstandingValidation(NamedTuple):
    facts: dict
    basis: dict
    intents: list
    topic_switch: bool

MIN_FACT_CONFIDENCE = 0.80
NUMBER_PATTERN = re.compile(r"(?<![A-Za-z0-9])\d+(?:,\d{3})*(?:\.\d+)?%?")

# English number words in grounded data ("valid for at least six months") are
# the same fact as the digit form a Chinese writer naturally produces ("至少6个月").
_WORD_NUMBERS = {
    "zero": "0", "one": "1", "two": "2", "three": "3", "four": "4", "five": "5",
    "six": "6", "seven": "7", "eight": "8", "nine": "9", "ten": "10",
    "eleven": "11", "twelve": "12", "thirteen": "13", "fourteen": "14",
    "fifteen": "15", "sixteen": "16", "seventeen": "17", "eighteen": "18",
    "nineteen": "19", "twenty": "20", "thirty": "30", "forty": "40",
    "fifty": "50", "sixty": "60", "seventy": "70", "eighty": "80", "ninety": "90",
}
_WORD_NUMBER_PATTERN = re.compile(r"\b(" + "|".join(_WORD_NUMBERS) + r")\b", re.I)


def _word_number_values(text: str) -> set[str]:
    return {_WORD_NUMBERS[word.casefold()] for word in _WORD_NUMBER_PATTERN.findall(text)}


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
    return UnderstandingValidation(
        {code: value for code, (value, _) in facts.items()}, basis,
        list(dict.fromkeys(result.intents)), result.topic_switch)


def validate_written_answer(candidate: AnswerBody, source: AnswerBody,
                            *, supported_text: str = "") -> None:
    """Reject new numeric claims and keep locked materials/limitations byte-for-byte.

    A number is supported when it already appears in the deterministic answer,
    or in ``supported_text`` — the grounded data (rule rows, fees, documents)
    that was fed to the writer alongside it. English number words in the data
    ("six months") count as support for their digit form ("6个月"). The writer
    may restate those numbers in prose; it still may not invent numbers absent
    from both.
    """
    source_text = " ".join([source.summary, *source.steps, *source.limitations])
    candidate_text = " ".join([candidate.summary, *candidate.steps])
    allowed = set(NUMBER_PATTERN.findall(source_text))
    if supported_text:
        allowed |= set(NUMBER_PATTERN.findall(supported_text))
        allowed |= _word_number_values(supported_text)
    unsupported_numbers = set(NUMBER_PATTERN.findall(candidate_text)) - allowed
    if unsupported_numbers:
        raise ValueError("writer introduced unsupported numeric facts: " + ", ".join(sorted(unsupported_numbers)))
    if candidate.materials != source.materials or candidate.limitations != source.limitations:
        raise ValueError("writer changed locked answer fields")
