"""Adapter that lets one migrated v2 domain run behind the existing Agent2 service."""

import re

from agent2.models import AnswerResult
from agent2.runner import RunOutput

from .contracts import OrchestrationInput


_CARD_ROLES = {"主推荐": "primary", "场景搭配": "supplement", "其他选择": "alternative"}
_CARD_NETWORK = re.compile(r"^(.+?)（(?:万事达卡|威士卡|银联卡|银行卡|Visa|Mastercard|UnionPay)）$")


def card_summaries_from_steps(steps, groups=()):
    """Keep the plan card aligned with the already verified customer-facing answer."""
    cards = []
    market_requirement = ""
    for index, line in enumerate(steps):
        if line.endswith("发行的卡") and index + 1 < len(steps):
            market_requirement = steps[index + 1]
        label, sep, value = line.partition("：")
        network = _CARD_NETWORK.fullmatch(line)
        if sep and label in _CARD_ROLES:
            cards.append({"product_name": value.strip(), "role": _CARD_ROLES[label]})
        elif network:
            cards.append({"product_name": network.group(1), "role": "candidate"})
        elif cards and sep and label in {"为什么适合你", "推荐理由", "年费", "主要权益",
                                           "申请前提", "申请限制"}:
            field = {"为什么适合你": "reason_text", "推荐理由": "reason_text",
                     "年费": "fee_text", "主要权益": "benefits_text",
                     "申请前提": "application_note", "申请限制": "application_note"}[label]
            cards[-1][field] = value.strip()
    by_name = {item.get("product_name"): (group, item)
               for group in groups for item in group.get("cards", [])}
    for card in cards:
        match = by_name.get(card["product_name"])
        if match:
            group, item = match
            card.update(card_id=item.get("card_id"), eligibility=item.get("eligibility"),
                        market=group.get("market"), preview=group.get("role") == "preview")
            if card["role"] == "candidate" and item.get("role"):
                card["role"] = item["role"]
        if not card.get("application_note") and market_requirement:
            card["application_note"] = market_requirement
    return cards


def run_credit_card(workflow, *, profile, text, conversation_facts, asked_fields,
                    message_id, structured_selection=False):
    profile_values = {code: field.value for code, field in profile.fields.items()
                      if field.status == "confirmed" and field.value is not None}
    profile_values.update(conversation_facts or {})
    request = OrchestrationInput(
        text=text,
        profile_id=profile.profile_id,
        profile_version=profile.version,
        profile_values=profile_values,
        conversation_intents=["credit_card"],
        asked_fields=list(asked_fields),
        input_mode="structured_selection" if structured_selection else "free_text",
    )
    orchestrated = workflow.run(request)
    unified = orchestrated.answer
    facts = {item.field_code: item.new_value for item in orchestrated.lifecycle.profile_change_candidates}
    basis = {item.field_code: item.basis for item in orchestrated.lifecycle.profile_change_candidates}
    advice = None
    advice_updates = []
    domain_payload = unified.domain_results[0].payload if unified.domain_results else {}
    groups = domain_payload.get("groups") or []
    cards = card_summaries_from_steps(unified.answer.steps, groups)
    if unified.status in {"answered", "needs_verification"} and (
            cards or unified.status == "answered"):
        advice = {"topic": "credit_card", "payload": {
            "summary": unified.answer.summary,
            "recommendations": domain_payload.get("recommendations", {}),
            "groups": groups,
            "cards": cards,
            "market": domain_payload.get("market"),
            "result_status": unified.status,
            "preview": any(group.get("role") == "preview" for group in groups)
                       or any(step.startswith("提前了解：") for step in unified.answer.steps),
            "trace_id": orchestrated.trace_id,
        }}
        advice_updates = ["credit_card"]
    result = AnswerResult(
        message_id=message_id,
        status=unified.status,
        profile_id=profile.profile_id,
        profile_version=profile.version,
        answer=unified.answer,
        missing_fields=unified.missing_fields,
        follow_up_questions=unified.follow_up_questions,
        next_actions=unified.next_actions,
        notices=unified.notices,
        advice_updates=advice_updates,
        synthetic=False,
        trace_id=orchestrated.trace_id,
        stage_ms=orchestrated.stage_ms,
    )
    read_ids = orchestrated.snapshot.knowledge_read_ids
    llm_models = [value for value in orchestrated.snapshot.model_versions.values() if value != "none"]
    llm_prompts = list(orchestrated.snapshot.prompt_versions.values())
    return RunOutput(
        result=result,
        turn_facts=facts,
        turn_basis=basis,
        topic="credit_card",
        knowledge_read_id=next(iter(read_ids.values()), None),
        model_version="+".join(dict.fromkeys(["credit-card-rules-1", *llm_models])),
        prompt_version="+".join(dict.fromkeys(["credit-card-template-1", *llm_prompts])),
        gate_version=orchestrated.snapshot.gate_version,
        advice=advice,
        stage_ms=orchestrated.stage_ms,
    )
