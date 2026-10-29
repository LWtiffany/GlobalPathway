"""Shared helpers for minimal closed-loop domain workflows (fx / bank_account)."""

from agent2 import clarify
from agent2.models import AnswerBody, FollowUpQuestion

from ..contracts import Domain, DomainResult, DomainTask, OrchestrationInput

COUNTRY_LABEL = {"UK": "英国", "US": "美国", "CN-HK": "中国香港"}


def merged_facts(task: DomainTask, request: OrchestrationInput) -> tuple[dict, dict]:
    facts = {**request.profile_values, **task.facts, **request.understood_facts}
    basis = {**task.basis, **request.understood_basis}
    return facts, basis


def catalog_question(field: str, profile_version: int) -> FollowUpQuestion:
    item = clarify.field_catalog()[field]
    input_type = "single_select" if item.get("options") else "text"
    if item.get("kind") == "multi_enum":
        input_type = "multi_select"
    policy = clarify.persistence_policy(field)
    persist_mode = "session_only" if policy["confirmation_mode"] == "never" else \
        "proposal" if policy["confirmation_mode"] == "proposal" else \
        "save_on_select" if item.get("options") else "proposal"
    return FollowUpQuestion(
        question_code=f"ask_{field}",
        field=field,
        text=item["question"],
        rule="minimal_domain_v1",
        profile_version=profile_version,
        input_type=input_type,
        options=list(item.get("options") or []),
        max_selections=item.get("max_selections"),
        persist_mode=persist_mode,
    )


def needs_input(task: DomainTask, domain: Domain, *, summary: str, missing: list[str],
                facts: dict, basis: dict, profile_version: int) -> DomainResult:
    field = missing[0]
    return DomainResult(
        task_id=task.task_id,
        domain=domain,
        status="needs_input",
        answer=AnswerBody(
            summary=summary,
            steps=["先确认会影响建议方向的关键信息，再给出准备清单。"],
        ),
        missing_fields=missing,
        follow_up_questions=[catalog_question(field, profile_version)],
        observed_facts=facts,
        observed_basis=basis,
    )


def answered(task: DomainTask, domain: Domain, *, summary: str, steps: list[str],
             limitations: list[str], facts: dict, basis: dict, next_actions: list[str],
             evidence_ref: str, payload: dict | None = None) -> DomainResult:
    return DomainResult(
        task_id=task.task_id,
        domain=domain,
        status="answered",
        answer=AnswerBody(summary=summary, steps=steps, limitations=limitations),
        evidence_refs=[evidence_ref],
        next_actions=next_actions,
        observed_facts=facts,
        observed_basis=basis,
        payload=payload or {},
        model_version="minimal-domain-rules-1",
    )
