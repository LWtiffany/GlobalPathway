"""Plan projections after the immutable answer snapshot has been constructed."""

from .contracts import (AdviceUpdate, LifecycleActions, OrchestrationInput, PlanItemCandidate,
                        ProfileChangeCandidate, TaskPlan, UnifiedAnswer)

PROFILE_FIELDS_BY_DOMAIN = {
    # Visa category inferred from a materials question is query-scoped.
    # A school named as the student's own school may be stored when the field
    # is empty; replacing an existing value still requires confirmation.
    "visa": {"school"},
    "credit_card": {"applicant_age", "applicant_age_band",
                    "annual_income_hkd", "annual_income_band_hkd", "asset_band",
                    "repayment_mode", "spending_scenarios", "study_card_focus",
                    "reward_preference",
                    "monthly_card_spend_band_hkd",
                    "mainland_payment_method",
                    "full_time_student", "premier_relationship", "hsbc_relationship_balance_band",
                    "hsbc_premier_path", "us_hsbc_account_path", "uk_hsbc_account_path",
                    "hk_resident"},
    "fx": {"destination_country", "remittance_purpose", "payment_deadline", "amount",
           "currency", "payer", "school"},
    "bank_account": {"applicant_identity",
                     "account_applicant_relation", "applicant_age", "uk_residency_years",
                     "application_location", "identity_document_type", "has_hk_identity_card",
                     "qualifying_course_offer", "tax_certificate_or_ssn", "guardian_present",
                     },
}


class DefaultLifecyclePlanner:
    def propose(self, request: OrchestrationInput, plan: TaskPlan,
                answer: UnifiedAnswer) -> LifecycleActions:
        advice: list[AdviceUpdate] = []
        profile: list[ProfileChangeCandidate] = []
        plan_items: list[PlanItemCandidate] = []
        suppressed: list[str] = []
        tasks = {task.task_id: task for task in plan.tasks}
        for result in answer.domain_results:
            task = tasks.get(result.task_id)
            if task is None:
                suppressed.append(f"unknown_task:{result.task_id}")
                continue
            # Bank-account answers remain useful while a decisive condition is
            # being collected: persist the focused product/materials/rules so
            # the plan card is populated next to the choice question.
            persist_partial_account = (
                task.domain == "bank_account" and result.status == "needs_input"
                and bool(result.payload.get("candidate_results"))
            )
            if ((result.status == "answered" or persist_partial_account)
                    and result.projection == "advice"):
                advice.append(AdviceUpdate(
                    domain=task.domain,
                    topic=task.intent,
                    source_task_id=task.task_id,
                    payload={"summary": result.answer.summary,
                             "steps": result.answer.steps,
                             "evidence_refs": result.evidence_refs,
                             "domain_payload": result.payload},
                ))
            if result.projection == "chat_only":
                suppressed.extend([
                    f"{result.task_id}:advice_update",
                    f"{result.task_id}:profile_change",
                    f"{result.task_id}:plan_item",
                ])
                continue
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
                                plan_item_candidates=plan_items,
                                suppressed_effects=suppressed)
