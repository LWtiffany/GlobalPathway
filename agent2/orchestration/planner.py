"""Deterministic multi-intent planner used until model-assisted planning is needed."""

import re

from agent2.intent import INTENTS, classify

from .contracts import Domain, DomainTask, OrchestrationInput, TaskPlan

PLANNER_VERSION = "rules-v2"
INTENT_DOMAIN: dict[str, Domain] = {
    "visa_documents": "visa",
    "remittance_tuition": "fx",
    "remittance_living": "fx",
    "overseas_spending": "fx",
    "hsbc_account": "bank_account",
    "credit_card": "credit_card",
}


class RuleTaskPlanner:
    """Find every supported intent and keep its first textual occurrence order."""

    version = PLANNER_VERSION

    async def plan(self, request: OrchestrationInput) -> TaskPlan:
        if request.input_mode == "structured_selection":
            tasks = [
                DomainTask(
                    task_id=f"task-{index}", domain=INTENT_DOMAIN[intent],
                    intent=intent, facts={}, basis={},
                )
                for index, intent in enumerate(dict.fromkeys(request.conversation_intents), 1)
                if intent in INTENT_DOMAIN
            ]
            return TaskPlan(
                tasks=tasks,
                unsupported_intents=[] if tasks else ["other"],
                planner_version=self.version,
            )

        classified = classify(request.text, asked_fields=request.asked_fields)
        credit_card_account_spans = [match.span() for match in re.finditer(r"信用卡\s*(?:开户|开卡)", request.text)]
        matches: list[tuple[int, str]] = []
        unsupported: list[str] = []
        for intent, pattern in INTENTS:
            match = pattern.search(request.text)
            if not match:
                continue
            if intent == "out_of_scope":
                unsupported.append(intent)
            elif intent in INTENT_DOMAIN:
                if intent == "hsbc_account" and any(start <= match.start() < end
                                                     for start, end in credit_card_account_spans):
                    continue
                matches.append((match.start(), intent))

        # Model understanding may recover paraphrases that the deterministic patterns miss.
        for intent in request.conversation_intents:
            if intent in INTENT_DOMAIN and not any(item[1] == intent for item in matches):
                matches.append((len(request.text) + len(matches), intent))

        if not matches and classified.intent in ("follow_up", "other"):
            for intent in request.conversation_intents:
                if intent in INTENT_DOMAIN:
                    matches.append((len(matches), intent))

        tasks: list[DomainTask] = []
        seen: set[str] = set()
        for _, intent in sorted(matches):
            if intent in seen:
                continue
            seen.add(intent)
            if request.input_mode == "structured_selection":
                facts, basis = {}, {}
            else:
                facts = {**classified.facts, **request.understood_facts}
                basis = {**classified.basis, **request.understood_basis}
            purpose = {"remittance_tuition": "tuition", "remittance_living": "living"}.get(intent)
            if purpose:
                facts["remittance_purpose"] = purpose
                basis["remittance_purpose"] = "学费" if purpose == "tuition" else "生活费"
            tasks.append(DomainTask(task_id=f"task-{len(tasks) + 1}", domain=INTENT_DOMAIN[intent],
                                    intent=intent, facts=facts, basis=basis))

        if not tasks and not unsupported:
            unsupported.append("other")
        return TaskPlan(tasks=tasks, unsupported_intents=unsupported, planner_version=self.version)
