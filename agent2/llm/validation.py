"""Application-side checks for model output before it enters business state."""

import re
from collections.abc import Callable
from typing import NamedTuple

from agent2.models import AnswerBody
from agent2.orchestration.contracts import OrchestrationInput

from .schemas import CreditCardAdvisorDraft, UnderstandingResult


def validate_card_advisor(draft: CreditCardAdvisorDraft, lines: dict[str, str],
                          source: AnswerBody) -> None:
    selected = [lines[key] for key in draft.evidence_ids if key in lines]
    if len(selected) != len(draft.evidence_ids) or len(set(draft.evidence_ids)) != len(draft.evidence_ids):
        raise ValueError("advisor cited unknown or duplicate evidence")
    numbers = set(NUMBER_PATTERN.findall(draft.analysis))
    allowed = set(NUMBER_PATTERN.findall(" ".join(selected)))
    if numbers - allowed:
        raise ValueError("advisor introduced an unsupported number")
    prohibited = ("保证获批", "一定获批", "确保获批", "保证通过", "无条件获批")
    if any(value in draft.analysis for value in prohibited):
        raise ValueError("advisor guaranteed approval")


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

# Real numeric claims are multi-char combinations (三万, 二十八, 五千). A lone
# 一/两 (两件事, 分两步) is a classifier, not a figure, and must not be blocked.
CHINESE_NUMBER_PATTERN = re.compile(r"[一二两三四五六七八九十百千万亿]{2,}")
LATIN_TOKEN_PATTERN = re.compile(r"[A-Za-z][A-Za-z0-9&.\-']{2,}")
PROMOTION_RISK_TERMS = (
    "保证", "包过", "必过", "秒批", "稳过", "免费办", "免年费", "首年免",
    "终身免", "赠送", "送积分", "送里程", "返现卡", "额外奖励", "100%通过", "无条件批",
)


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
        value = item.value
        if item.update_mode != "replace":
            # Incremental edits are meaningful only for multi-value fields.
            # Merge against server-side state; profile values are deliberately
            # not sent to the model.
            if not isinstance(value, list):
                continue
            current = request.profile_values.get(item.field_code) or []
            if not isinstance(current, list):
                continue
            if item.update_mode == "add":
                value = list(dict.fromkeys([*current, *value]))
            else:
                removed = set(value)
                value = [entry for entry in current if entry not in removed]
                if not value:
                    # Clearing a durable field requires an explicit clear flow;
                    # do not smuggle an invalid empty multi-select through facts.
                    continue
        if value_validator(item.field_code, value):
            continue
        if item.field_code not in facts or item.confidence > facts[item.field_code][1]:
            facts[item.field_code] = (value, item.confidence)
            basis[item.field_code] = evidence
    return UnderstandingValidation(
        {code: value for code, (value, _) in facts.items()}, basis,
        list(dict.fromkeys(result.intents)), result.topic_switch)


def validate_written_answer(candidate: AnswerBody, source: AnswerBody, *,
                            supported_text: str = "", user_text: str = "",
                            profile_name: str = "") -> None:
    """Reject new numeric claims, unverified named entities, promotion-sounding
    claims, and keep locked materials/limitations byte-for-byte.

    A number is supported when it already appears in the deterministic answer,
    in ``supported_text`` (grounded data fed to the writer), or in the user's
    own words this turn (``user_text``; already gate-checked by understanding).
    English number words in grounded data ("six months") count as support for
    their digit form ("6个月"), and Chinese numerals count when the source or
    the user wrote them. The writer may restate supported numbers in prose;
    it still may not invent numbers absent from all sources.
    """
    source_text = " ".join([source.summary, *source.steps, *source.limitations])
    allowed_text = " ".join([source_text, supported_text or "", user_text or ""])
    candidate_text = " ".join([candidate.summary, *candidate.steps])
    allowed = (set(NUMBER_PATTERN.findall(allowed_text))
               | set(CHINESE_NUMBER_PATTERN.findall(allowed_text)))
    if supported_text:
        allowed |= _word_number_values(supported_text)
    unsupported_numbers = ((set(NUMBER_PATTERN.findall(candidate_text))
                            | set(CHINESE_NUMBER_PATTERN.findall(candidate_text)))
                           - allowed)
    if unsupported_numbers:
        raise ValueError("writer introduced unsupported numeric facts: " + ", ".join(sorted(unsupported_numbers)))
    # Product names and named entities are Latin tokens in this workspace
    # (HSBC, Red, Visa, Gold, ...). A token absent from the verified text is
    # treated as an invented product or provider and rejected outright; the
    # profile's own name is the one sanctioned extra entity.
    allowed_tokens = (set(LATIN_TOKEN_PATTERN.findall(source_text))
                      | set(LATIN_TOKEN_PATTERN.findall(profile_name or "")))
    unsupported_tokens = sorted(set(LATIN_TOKEN_PATTERN.findall(candidate_text)) - allowed_tokens)
    if unsupported_tokens:
        raise ValueError("writer mentioned unverified named entities: " + ", ".join(unsupported_tokens))
    # Regulatory/promotional claims (guarantees, waivers, giveaways) may only
    # appear when the verified answer itself states them.
    unsupported_risks = sorted(
        term for term in PROMOTION_RISK_TERMS if term in candidate_text and term not in source_text)
    if unsupported_risks:
        raise ValueError("writer introduced promotional claims: " + ", ".join(unsupported_risks))
    if candidate.materials != source.materials or candidate.limitations != source.limitations:
        raise ValueError("writer changed locked answer fields")
