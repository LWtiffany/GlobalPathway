"""Adapter that lets migrated v2 domains run behind the existing Agent2 service."""

import re
import uuid

from agent2 import clarify
from agent2.models import AnswerBody, AnswerResult
from agent2.runner import RunOutput

from .composer import (DOMAIN_LABEL, is_necessity_question, necessity_preamble,
                       necessity_summary)
from .contracts import OrchestrationInput
from .planner import INTENT_DOMAIN


def _new_message_id():
    return str(uuid.uuid4())

_CARD_ROLES = {"主推荐": "primary", "初步选择": "primary",
               "场景搭配": "supplement", "附加推荐": "supplement",
               "其他选择": "alternative"}
_CARD_NETWORK = re.compile(r"^(.+?)（(?:万事达卡|威士卡|银联卡|银行卡|Visa|Mastercard|UnionPay)）$")

# Session topic values must match store CHECK (remittance/account/visa/credit_card).
INTENT_SESSION_TOPIC = {
    "credit_card": "credit_card",
    "remittance_tuition": "remittance",
    "remittance_living": "remittance",
    "overseas_spending": "remittance",
    "hsbc_account": "account",
    "visa_documents": "visa",
}
V2_INTENTS = frozenset(INTENT_SESSION_TOPIC)
TOPIC_PRIORITY = ("credit_card", "remittance", "account", "visa")


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
            cards.append({"product_name": value.strip(), "role": _CARD_ROLES[label],
                          "display_label": label})
        elif network:
            cards.append({"product_name": network.group(1), "role": "candidate"})
        elif cards and sep and label in {"为什么适合你", "为什么适合您", "推荐理由", "年费", "主要权益",
                                           "申请前提", "申请限制"}:
            field = {"为什么适合你": "reason_text", "为什么适合您": "reason_text", "推荐理由": "reason_text",
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


def session_topic_for_intents(intents: list[str]) -> str | None:
    topics = {INTENT_SESSION_TOPIC[intent] for intent in intents if intent in INTENT_SESSION_TOPIC}
    for topic in TOPIC_PRIORITY:
        if topic in topics:
            return topic
    return None


def intents_for_session_topic(topic: str | None, conversation_facts: dict | None = None) -> list[str]:
    """Restore planner intents when a structured follow-up continues a v2 topic."""
    facts = conversation_facts or {}
    if topic == "credit_card":
        return ["credit_card"]
    if topic == "account":
        return ["hsbc_account"]
    if topic == "remittance":
        mode = facts.get("fx_mode") or facts.get("remittance_purpose")
        if mode in {"tuition", "remittance_tuition"}:
            return ["remittance_tuition"]
        if mode in {"living", "remittance_living"}:
            return ["remittance_living"]
        if mode == "overseas":
            return ["overseas_spending"]
        purpose = facts.get("remittance_purpose")
        if purpose == "tuition":
            return ["remittance_tuition"]
        if purpose == "living":
            return ["remittance_living"]
        return ["overseas_spending"]
    if topic == "visa":
        return ["visa_documents"]
    return []


def intents_for_confirmed_fields(confirmed_fields: dict, active_intents: list[str] | None) -> list[str]:
    """When the user answers a follow-up chip, only re-run domains that own those fields.

    Multi-intent turns used to keep every open intent on every chip click, which
    re-emitted bank-account advice after the user only confirmed want_credit_card.
    """
    active = list(active_intents or [])
    if not confirmed_fields or not active:
        return active
    from agent2 import clarify
    owned: set[str] = set()
    for code in confirmed_fields:
        owned.update(clarify.field_definitions().get(code, {}).get("domains") or [])
    if not owned:
        return active
    narrowed = [
        intent for intent in active
        if intent in owned or INTENT_DOMAIN.get(intent) in owned
    ]
    return narrowed or active


def run_v2(workflow, *, profile, text, conversation_facts, asked_fields, message_id,
           structured_selection=False, conversation_intents=None,
           understood_facts=None, understood_basis=None, understanding_applied=False,
           understanding_model_used=False,
           llm_intents=None, domain_state=None, llm_topic_switch=False,
           session_topic=None, session_last_user_text="", session_last_assistant_text="",
           session_pending_questions=(), expected_profile_version=None):
    """Run Agent2 v2 for any migrated domain intents (card / fx / account)."""
    intents = [intent for intent in dict.fromkeys(conversation_intents or [])
               if intent in INTENT_DOMAIN or intent in V2_INTENTS]
    # Structured follow-ups must keep a domain; free-text may be empty for refusal turns.
    if not intents and structured_selection:
        intents = ["credit_card"]
    # Legacy profiles may still contain fields that are now explicitly scoped
    # to a conversation (for example application_region).  Do not let those
    # stale values silently steer a new account/card question.
    profile_values = {
        code: field.value for code, field in profile.fields.items()
        if field.status == "confirmed" and field.value is not None
        and clarify.persistence_policy(code)["storage_class"] in {"core_profile", "preference"}
    }
    profile_values.update(conversation_facts or {})
    request = OrchestrationInput(
        text=text,
        profile_id=profile.profile_id,
        profile_version=profile.version,
        profile_values=profile_values,
        conversation_intents=intents,
        asked_fields=list(asked_fields),
        input_mode="structured_selection" if structured_selection else "free_text",
        understood_facts=dict(understood_facts or {}),
        understood_basis=dict(understood_basis or {}),
        understanding_applied=understanding_applied,
        understanding_model_used=understanding_model_used,
        llm_intents=list(dict.fromkeys(llm_intents or [])),
        llm_topic_switch=llm_topic_switch,
        session_topic=session_topic,
        session_last_user_text=session_last_user_text,
        session_last_assistant_text=session_last_assistant_text,
        session_pending_questions=list(session_pending_questions),
        domain_state=dict(domain_state or {}),
        expected_profile_version=expected_profile_version,
    )
    orchestrated = workflow.run(request)
    unified = orchestrated.answer
    turn_facts, turn_basis = {}, {}
    for result in unified.domain_results:
        turn_facts.update(result.observed_facts)
        turn_basis.update(result.observed_basis)
    for item in orchestrated.lifecycle.profile_change_candidates:
        turn_facts[item.field_code] = item.new_value
        turn_basis[item.field_code] = item.basis

    planned_intents = [task.intent for task in orchestrated.snapshot.task_plan.tasks]
    session_topic = session_topic_for_intents(planned_intents or intents)
    advice_items = []
    advice_updates = []
    for result in unified.domain_results:
        # Generic explanations, comparisons and troubleshooting are normal chat
        # answers, not a new recommendation snapshot.  Never let them replace a
        # saved plan card merely because their status is answered.
        if result.projection == "chat_only":
            continue
        partial_account = (result.domain == "bank_account"
                           and result.status == "needs_input"
                           and bool(result.payload.get("candidate_results")))
        if result.status not in {"answered", "needs_verification"} and not partial_account:
            continue
        topic = INTENT_SESSION_TOPIC.get(
            next((task.intent for task in orchestrated.snapshot.task_plan.tasks
                  if task.domain == result.domain), ""),
            session_topic,
        )
        # Per-domain payload: keep each side-panel topic on its own answer, not the
        # merged multi-intent steps (which would make every panel look identical).
        payload = {
            "summary": result.answer.summary,
            "steps": result.answer.steps,
            "result_status": result.status,
            "trace_id": orchestrated.trace_id,
            "domain_payload": result.payload,
            "materials": [m.model_dump(mode="json") for m in result.answer.materials],
            "citations": [c.model_dump(mode="json") for c in result.citations],
            "fx_mode": result.payload.get("fx_mode"),
            "remittance_purpose": result.payload.get("remittance_purpose"),
        }
        if result.domain == "credit_card":
            groups = result.payload.get("groups") or []
            cards = card_summaries_from_steps(result.answer.steps, groups)
            if not cards and result.status != "answered":
                continue
            payload.update({
                "recommendations": result.payload.get("recommendations", {}),
                "groups": groups,
                "cards": cards,
                "market": result.payload.get("market"),
                "preview": any(group.get("role") == "preview" for group in groups)
                           or any(step.startswith("提前了解：") for step in result.answer.steps),
            })
        task = next((task for task in orchestrated.snapshot.task_plan.tasks
                     if task.task_id == result.task_id), None)
        advice_items.append({
            "topic": topic,
            "payload": payload,
            "source_task_id": result.task_id,
            "intent": task.intent if task else None,
            "knowledge_read_id": result.knowledge_read_id,
            "model_version": result.model_version,
        })
        if topic not in advice_updates:
            advice_updates.append(topic)

    message_parts = _split_chat_parts(
        text=text,
        message_id=message_id,
        profile=profile,
        unified=unified,
        advice_updates=advice_updates,
        trace_id=orchestrated.trace_id,
        stage_ms=orchestrated.stage_ms,
    )
    result = message_parts[-1]
    read_ids = orchestrated.snapshot.knowledge_read_ids
    llm_models = [value for value in orchestrated.snapshot.model_versions.values() if value != "none"]
    llm_prompts = list(orchestrated.snapshot.prompt_versions.values())
    domains = sorted({task.domain for task in orchestrated.snapshot.task_plan.tasks})
    return RunOutput(
        result=result,
        turn_facts=turn_facts,
        turn_basis=turn_basis,
        topic=session_topic,
        knowledge_read_id=next(iter(read_ids.values()), None),
        model_version="+".join(dict.fromkeys([f"v2-{'-'.join(domains) or 'empty'}", *llm_models])),
        prompt_version="+".join(dict.fromkeys(["v2-compose-1", *llm_prompts])),
        gate_version=orchestrated.snapshot.gate_version,
        advice=advice_items[0] if advice_items else None,
        advice_items=advice_items,
        active_intents=planned_intents,
        message_parts=message_parts,
        stage_ms=orchestrated.stage_ms,
        profile_change_candidates=list(orchestrated.lifecycle.profile_change_candidates),
    )


def _split_chat_parts(*, text, message_id, profile, unified, advice_updates, trace_id, stage_ms):
    """Turn one multi-domain run into 2–3 chat bubbles with original domain formatting.

    1) Optional「先说要不要」intro when the user asked a need question
    2) One bubble per domain using that domain's own summary/steps/materials/citations
    Follow-ups, notices, and advice_updates stay on the last bubble.
    """
    results = list(unified.domain_results)
    domains = [item.domain for item in results]
    ask_need = is_necessity_question(text)
    split = ask_need or len(results) > 1
    if not split or not results:
        citations, claims, conflicts = [], [], []
        seen = set()
        for domain_result in results:
            for citation in domain_result.citations:
                if citation.citation_id in seen:
                    continue
                seen.add(citation.citation_id)
                citations.append(citation)
            claims.extend(domain_result.claims)
            for conflict in domain_result.conflicts:
                if not any(existing.document_code == conflict.document_code
                           for existing in conflicts):
                    conflicts.append(conflict)
        return [AnswerResult(
            message_id=message_id,
            status=unified.status,
            profile_id=profile.profile_id,
            profile_version=profile.version,
            answer=unified.answer,
            claims=claims,
            citations=citations,
            conflicts=conflicts,
            missing_fields=unified.missing_fields,
            follow_up_questions=unified.follow_up_questions,
            next_actions=unified.next_actions,
            notices=unified.notices,
            advice_updates=advice_updates,
            synthetic=any(c.synthetic for c in citations),
            trace_id=trace_id,
            stage_ms=stage_ms,
        )]

    parts: list[AnswerResult] = []
    if ask_need:
        parts.append(AnswerResult(
            message_id=_new_message_id(),
            status="answered",
            profile_id=profile.profile_id,
            profile_version=profile.version,
            answer=AnswerBody(
                summary=necessity_summary(domains),
                steps=necessity_preamble(domains),
            ),
            synthetic=False,
            trace_id=trace_id,
            stage_ms={},
        ))

    if unified.journey:
        parts.append(AnswerResult(
            message_id=_new_message_id(),
            status="answered",
            profile_id=profile.profile_id,
            profile_version=profile.version,
            answer=AnswerBody(
                summary="这些事项可以按下面的顺序推进；标注可并行的事项不必互相等待。",
                steps=[
                    f"{step.order}. {step.label}：{step.guidance}"
                    for step in unified.journey
                ],
            ),
            synthetic=False,
            trace_id=trace_id,
            stage_ms={},
        ))

    for index, domain_result in enumerate(results):
        last = index == len(results) - 1
        label = DOMAIN_LABEL.get(domain_result.domain, domain_result.domain)
        summary = domain_result.answer.summary or ""
        if summary and not summary.startswith(label):
            summary = f"{label}：{summary}"
        # Keep the service-allocated id on the last bubble (API / idempotency).
        mid = message_id if last else _new_message_id()
        parts.append(AnswerResult(
            message_id=mid,
            status=domain_result.status if not last else unified.status,
            profile_id=profile.profile_id,
            profile_version=profile.version,
            answer=AnswerBody(
                summary=summary,
                steps=list(domain_result.answer.steps),
                materials=list(domain_result.answer.materials),
                limitations=list(domain_result.answer.limitations),
            ),
            claims=list(domain_result.claims),
            citations=list(domain_result.citations),
            conflicts=list(domain_result.conflicts),
            missing_fields=list(domain_result.missing_fields) if last else [],
            follow_up_questions=list(unified.follow_up_questions) if last else [],
            next_actions=list(domain_result.next_actions),
            notices=list(unified.notices) if last else list(domain_result.notices),
            advice_updates=list(advice_updates) if last else [],
            synthetic=any(c.synthetic for c in domain_result.citations),
            trace_id=trace_id,
            stage_ms=stage_ms if last else {},
        ))
    return parts


def run_credit_card(workflow, **kwargs):
    """Backward-compatible entry that ensures credit_card remains in the plan."""
    intents = list(dict.fromkeys(kwargs.pop("conversation_intents", None) or ["credit_card"]))
    if "credit_card" not in intents:
        intents.append("credit_card")
    return run_v2(workflow, conversation_intents=intents, **kwargs)
