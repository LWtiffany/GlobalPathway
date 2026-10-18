"""Plan projections after the immutable answer snapshot has been constructed."""

from .contracts import (AdviceUpdate, LifecycleActions, OrchestrationInput, PlanItemCandidate,
                        ProfileChangeCandidate, TaskPlan, UnifiedAnswer)

PROFILE_FIELDS_BY_DOMAIN = {
    "credit_card": {"application_region", "applicant_age", "applicant_age_band",
                    "annual_income_hkd", "annual_income_band_hkd", "asset_band",
                    "repayment_mode", "spending_scenarios", "reward_preference",
                    "monthly_card_spend_band_hkd",
                    "mainland_payment_method",
                    "full_time_student", "premier_relationship", "hsbc_relationship_balance_band",
                    "hsbc_premier_path", "hk_resident"},
}


class DefaultLifecyclePlanner:
    def propose(self, request: OrchestrationInput, plan: TaskPlan,
                answer: UnifiedAnswer) -> LifecycleActions:
        advice: list[AdviceUpdate] = []
        profile: list[ProfileChangeCandidate] = []
        plan_items: list[PlanItemCandidate] = []
        for task, result in zip(plan.tasks, answer.domain_results):
            if result.status == "answered":
                advice.append(AdviceUpdate(
                    domain=task.domain,
                    topic=task.intent,
                    source_task_id=task.task_id,
                    payload={"summary": result.answer.summary,
                             "steps": result.answer.steps,
                             "evidence_refs": result.evidence_refs,
                             "domain_payload": result.payload},
                ))
            observed = {**task.facts, **result.observed_facts}
            basis = {**task.basis, **result.observed_basis}
            allowed = PROFILE_FIELDS_BY_DOMAIN.get(task.domain)
            if allowed is not None:
                observed = {field: value for field, value in observed.items() if field in allowed}
            for field, value in observed.items():
                if request.profile_values.get(field) != value and not any(p.field_code == field for p in profile):
                    profile.append(ProfileChangeCandidate(
                        field_code=field,
                        old_value=request.profile_values.get(field),
                        new_value=value,
                        basis=basis.get(field, "本轮用户陈述"),
                    ))
            for action in result.next_actions:
                plan_items.append(PlanItemCandidate(text=action, source_task_id=task.task_id))
        return LifecycleActions(advice_updates=advice, profile_change_candidates=profile,
                                plan_item_candidates=plan_items)
