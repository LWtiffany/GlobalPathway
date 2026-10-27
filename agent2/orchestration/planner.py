"""Deterministic multi-intent planner used until model-assisted planning is needed."""

import re

from agent2.intent import INTENTS, classify

from .contracts import (Domain, DomainTask, IntentHit, OrchestrationInput, RoutingAudit,
                        TaskPlan)

PLANNER_VERSION = "rules-v4"
INTENT_DOMAIN: dict[str, Domain] = {
    "visa_documents": "visa",
    "remittance_tuition": "fx",
    "remittance_living": "fx",
    "overseas_spending": "fx",
    "hsbc_account": "bank_account",
    "credit_card": "credit_card",
}

# 「信用卡开户/开卡」整段只算信用卡，避免再拆出 bank_account（不含单独「办卡+开户」并列）。
_CARD_ACCOUNT = re.compile(r"(?:信用卡|credit\s*card)\s*(?:开户|开卡)", re.I)

RULES_CONFIDENCE = 0.95
LLM_CONFIDENCE = 0.80
SESSION_CONFIDENCE = 1.0
# Drop LLM-only recoveries below this threshold (rules/session always keep).
MIN_LLM_ROUTE_CONFIDENCE = 0.80


class RuleTaskPlanner:
    """Find every supported intent and keep its first textual occurrence order."""

    version = PLANNER_VERSION

    async def plan(self, request: OrchestrationInput) -> TaskPlan:
        if request.input_mode == "structured_selection":
            intents = [intent for intent in dict.fromkeys(request.conversation_intents)
                       if intent in INTENT_DOMAIN]
            tasks = [
                DomainTask(
                    task_id=f"task-{index}", domain=INTENT_DOMAIN[intent],
                    intent=intent, facts={}, basis={},
                    confidence=SESSION_CONFIDENCE, source="session",
                )
                for index, intent in enumerate(intents, 1)
            ]
            audit = RoutingAudit(
                session_intents=list(intents),
                selected_intents=list(intents),
                unsupported_intents=[] if tasks else ["other"],
                refusal_reason=None if tasks else "other",
            )
            return TaskPlan(
                tasks=tasks,
                unsupported_intents=[] if tasks else ["other"],
                planner_version=self.version,
                routing_audit=audit,
            )

        classified = classify(request.text, asked_fields=request.asked_fields)
        card_account_spans = [match.span() for match in _CARD_ACCOUNT.finditer(request.text)]
        rules_hits: list[IntentHit] = []
        matches: list[tuple[int, str, str, float, str | None]] = []  # start, intent, source, conf, evidence
        unsupported: list[str] = []

        for intent, pattern in INTENTS:
            match = pattern.search(request.text)
            if not match:
                continue
            evidence = match.group(0)
            if intent == "out_of_scope":
                unsupported.append(intent)
                rules_hits.append(IntentHit(
                    intent=intent, source="rules", confidence=RULES_CONFIDENCE, evidence=evidence))
                continue
            if intent not in INTENT_DOMAIN:
                continue
            if intent == "hsbc_account" and any(left <= match.start() < right
                                                 for left, right in card_account_spans):
                continue
            rules_hits.append(IntentHit(
                intent=intent, source="rules", confidence=RULES_CONFIDENCE, evidence=evidence))
            matches.append((match.start(), intent, "rules", RULES_CONFIDENCE, evidence))

        # Only intents explicitly returned by understanding count as llm recoveries.
        ruled = {item[1] for item in matches}
        llm_hits: list[IntentHit] = []
        for intent in dict.fromkeys(request.llm_intents):
            if intent not in INTENT_DOMAIN or intent in ruled:
                continue
            if intent == "hsbc_account" and card_account_spans:
                continue
            if LLM_CONFIDENCE < MIN_LLM_ROUTE_CONFIDENCE:
                continue
            hit = IntentHit(intent=intent, source="llm", confidence=LLM_CONFIDENCE, evidence=None)
            llm_hits.append(hit)
            matches.append((len(request.text) + len(matches), intent, "llm", LLM_CONFIDENCE, None))

        session_seed: list[str] = []
        if not matches and classified.intent in ("follow_up", "other"):
            for intent in request.conversation_intents:
                if intent in INTENT_DOMAIN:
                    session_seed.append(intent)
                    matches.append((len(matches), intent, "session", SESSION_CONFIDENCE, None))

        tasks: list[DomainTask] = []
        seen: set[str] = set()
        for _, intent, source, confidence, _evidence in sorted(matches, key=lambda item: item[0]):
            if intent in seen:
                continue
            seen.add(intent)
            facts = {**classified.facts, **request.understood_facts}
            basis = {**classified.basis, **request.understood_basis}
            purpose = {"remittance_tuition": "tuition", "remittance_living": "living"}.get(intent)
            if purpose:
                facts["remittance_purpose"] = purpose
                basis["remittance_purpose"] = "学费" if purpose == "tuition" else "生活费"
            tasks.append(DomainTask(
                task_id=f"task-{len(tasks) + 1}", domain=INTENT_DOMAIN[intent],
                intent=intent, facts=facts, basis=basis,
                confidence=confidence, source=source,  # type: ignore[arg-type]
            ))

        if not tasks and not unsupported:
            unsupported.append("other")

        refusal_reason = None
        if not tasks and "out_of_scope" in unsupported:
            refusal_reason = "out_of_scope"
        elif not tasks and "other" in unsupported:
            refusal_reason = "other"
        elif tasks and unsupported:
            refusal_reason = "partial_out_of_scope"

        audit = RoutingAudit(
            rules_hits=rules_hits,
            llm_intents=llm_hits,
            session_intents=session_seed,
            selected_intents=[task.intent for task in tasks],
            unsupported_intents=list(unsupported),
            refusal_reason=refusal_reason,
        )
        return TaskPlan(
            tasks=tasks, unsupported_intents=unsupported, planner_version=self.version,
            routing_audit=audit,
        )
