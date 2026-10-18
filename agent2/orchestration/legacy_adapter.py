"""Adapter that lets one migrated v2 domain run behind the existing Agent2 service."""

from agent2.models import AnswerResult
from agent2.runner import RunOutput

from .contracts import OrchestrationInput


def run_credit_card(workflow, *, profile, text, conversation_facts, asked_fields,
                    message_id):
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
    )
    orchestrated = workflow.run(request)
    unified = orchestrated.answer
    facts = {item.field_code: item.new_value for item in orchestrated.lifecycle.profile_change_candidates}
    basis = {item.field_code: item.basis for item in orchestrated.lifecycle.profile_change_candidates}
    synthetic = any(note.code == "synthetic_card_catalog" for note in unified.notices)
    advice = None
    advice_updates = []
    domain_payload = unified.domain_results[0].payload if unified.domain_results else {}
    if unified.status == "answered":
        advice = {"topic": "credit_card", "payload": {
            "summary": unified.answer.summary,
            "recommendations": domain_payload.get("recommendations", {}),
            "market": domain_payload.get("market"),
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
        synthetic=synthetic,
        trace_id=orchestrated.trace_id,
        stage_ms=orchestrated.stage_ms,
    )
    read_ids = orchestrated.snapshot.knowledge_read_ids
    return RunOutput(
        result=result,
        turn_facts=facts,
        turn_basis=basis,
        topic="credit_card",
        knowledge_read_id=next(iter(read_ids.values()), None),
        model_version="credit-card-rules-1",
        prompt_version="credit-card-template-1",
        gate_version=orchestrated.snapshot.gate_version,
        advice=advice,
        stage_ms=orchestrated.stage_ms,
    )
