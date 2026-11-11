"""Deterministic multi-intent planner used until model-assisted planning is needed."""

import re

from agent2.intent import (INTENTS, classify, is_account_only_card_mention,
                           is_ambiguous_bank_card, is_investment_account)

from .contracts import (Domain, DomainTask, IntentHit, OrchestrationInput, RoutingAudit,
                        TaskPlan)

PLANNER_VERSION = "rules-v7"
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
_ORDER_FLOW = re.compile(r"先后|顺序|先办|再办|先做|下一步", re.I)

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
        ambiguous_card = is_ambiguous_bank_card(request.text)
        account_only_card = is_account_only_card_mention(request.text)
        investment_account = is_investment_account(request.text)
        banking_intents = {"hsbc_account", "credit_card"}
        # Session baseline excludes this turn's model recoveries: understanding
        # merges them into conversation_intents before planning, but stickiness
        # must reflect committed dialogue state only.
        session_intents = [intent for intent in dict.fromkeys(request.conversation_intents)
                           if intent in INTENT_DOMAIN
                           and intent not in set(request.llm_intents)]
        session_domains = {INTENT_DOMAIN[intent] for intent in session_intents}
        sticky_banking = list(dict.fromkeys(
            intent for intent in session_intents if intent in banking_intents))
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
            if intent == "hsbc_account" and investment_account:
                continue
            order_flow = bool(_ORDER_FLOW.search(request.text))
            if intent == "credit_card" and (ambiguous_card or account_only_card):
                # 交学费/开户/办卡先后：允许办卡进入信用卡域做顺序建议。
                if not (order_flow and re.search(r"办卡|信用卡", request.text)):
                    continue
            if intent == "hsbc_account" and any(left <= match.start() < right
                                                 for left, right in card_account_spans):
                continue
            rules_hits.append(IntentHit(
                intent=intent, source="rules", confidence=RULES_CONFIDENCE, evidence=evidence))
            matches.append((match.start(), intent, "rules", RULES_CONFIDENCE, evidence))

        # Only intents explicitly returned by understanding count as llm recoveries.
        ruled = {item[1] for item in matches}
        ruled_banking = ruled & banking_intents
        dropped_llm: list[str] = []
        llm_hits: list[IntentHit] = []
        for intent in dict.fromkeys(request.llm_intents):
            if intent not in INTENT_DOMAIN or intent in ruled:
                continue
            # Account and credit-card are sibling domains. A model recovery may
            # fill a rule miss, but it must not contradict an explicit cue in
            # this turn (e.g. 「账户类型推荐」 can never add credit_card).
            if len(ruled_banking) == 1 and intent in banking_intents \
                    and intent not in ruled_banking:
                continue
            if ambiguous_card and intent in {"hsbc_account", "credit_card"}:
                continue
            if intent == "credit_card" and account_only_card:
                continue
            if intent == "hsbc_account" and card_account_spans:
                continue
            if intent == "hsbc_account" and investment_account:
                continue
            # Sticky backstop: with an open session topic and no explicit cue in
            # this turn, a model-only cross-domain recovery must not hijack the
            # dialogue. Switching requires a rules hit or a topic_switch verdict.
            if session_intents and not rules_hits and not request.llm_topic_switch \
                    and INTENT_DOMAIN[intent] not in session_domains:
                dropped_llm.append(intent)
                continue
            if LLM_CONFIDENCE < MIN_LLM_ROUTE_CONFIDENCE:
                continue
            hit = IntentHit(intent=intent, source="llm", confidence=LLM_CONFIDENCE, evidence=None)
            llm_hits.append(hit)
            matches.append((len(request.text) + len(matches), intent, "llm", LLM_CONFIDENCE, None))

        session_seed: list[str] = []
        # A generic 「开卡 / 还有别的推荐吗」inherits the single active banking
        # domain. With no active domain (or two active domains), it remains
        # ambiguous and the composer asks the user to choose.
        if ambiguous_card and len(sticky_banking) == 1 \
                and not ({item[1] for item in matches} & banking_intents):
            sticky = sticky_banking[0]
            session_seed.append(sticky)
            matches.append((0, sticky, "session", SESSION_CONFIDENCE, None))
        if not matches and not ambiguous_card and classified.intent in ("follow_up", "other"):
            for intent in session_intents:
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
            unsupported.append("ambiguous_card" if ambiguous_card else "other")

        refusal_reason = None
        if not tasks and "out_of_scope" in unsupported:
            refusal_reason = "out_of_scope"
        elif not tasks and "other" in unsupported:
            refusal_reason = "other"
        elif not tasks and "ambiguous_card" in unsupported:
            refusal_reason = "ambiguous_card"
        elif tasks and unsupported:
            refusal_reason = "partial_out_of_scope"

        audit = RoutingAudit(
            rules_hits=rules_hits,
            llm_intents=llm_hits,
            dropped_llm_intents=dropped_llm,
            session_intents=session_seed,
            selected_intents=[task.intent for task in tasks],
            unsupported_intents=list(unsupported),
            refusal_reason=refusal_reason,
        )
        return TaskPlan(
            tasks=tasks, unsupported_intents=unsupported, planner_version=self.version,
            routing_audit=audit,
        )
