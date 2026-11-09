"""FX / remittance / overseas-spend workflow grounded on Agent1 FX policy quotes.

Tuition remittance answers also pull SCHOOL/PROGRAM/TUITION from RDS when the
profile names a school (and optionally a course) — amounts are never hardcoded.
"""

from __future__ import annotations

import json
from agent2.fx_knowledge import (Agent1FxPolicyRead, FxPolicyBundle, FxPolicyLoader,
                                 PURPOSE_LABEL, build_fx_answer_parts)
from agent2.llm.contracts import StructuredModel
from agent2.llm.prompts import PromptRenderer
from agent2.llm.schemas import WrittenAnswer
from agent2.llm.validation import validate_written_answer
from agent2.models import AnswerBody, Claim, Citation, Material, Notice
from agent2.tuition_knowledge import (TuitionContext, TuitionResolver, compare_to_quota,
                                      format_rate_line, resolve_tuition)
from agent2.tuition_payment_advice import enrich_tuition_advice_steps

from ..contracts import DomainResult, DomainTask, OrchestrationInput
from .minimal import COUNTRY_LABEL, merged_facts, needs_input

PROMPT_VERSION = "write-fx-answer-v2"
MODEL_VERSION_RULES = "fx-policy-rules-2"
REMITTER_COUNTRY = "CN"

_PURPOSE = {
    "remittance_tuition": "tuition",
    "remittance_living": "living",
}


def _limitations(*, mode: str, destination: str, tuition: TuitionContext | None) -> list[str]:
    notes = [
        "依据为中国内地个人留学购汇工作底稿中的政策摘录，办理以银行/外汇局当时规则为准。",
        "不含实时汇率、手续费或可成交报价。",
        f"收款地按{destination}整理；若汇款人非中国内地居民，规则可能不同。",
    ]
    if mode == "overseas":
        notes.append("境外消费另受发卡行与当地支付网络规则约束，本回答只覆盖购汇额度与用途材料要点。")
    if tuition and tuition.rates:
        urls = sorted({r.source_url for r in tuition.rates if r.source_url})
        notes.append("学费数字来自工作库中的学校公布学费目录；实际应缴以学校缴费通知书/账单为准。")
        if urls:
            notes.append("学费目录来源：" + "；".join(urls[:3]))
        if tuition.note:
            notes.append(tuition.note)
    elif tuition and tuition.note:
        notes.append(tuition.note)
    return notes


def _tuition_steps(tuition: TuitionContext, *, invoice_amount, invoice_currency: str | None,
                   quota_amount, quota_currency: str | None) -> list[str]:
    steps: list[str] = []
    if tuition.match in {"ambiguous_school", "ambiguous_program", "not_found", "none"}:
        return steps
    if tuition.school_name and tuition.rates:
        where = tuition.school_name
        if tuition.program_name:
            where = f"{tuition.school_name} / {tuition.program_name}"
        # Prefer distinct latest academic_year rows; keep all fee_status variants for that year.
        years = [r.academic_year for r in tuition.rates if r.academic_year]
        focus_year = years[0] if years else None
        shown = [r for r in tuition.rates
                 if focus_year is None or r.academic_year == focus_year][:6]
        steps.append(
            f"工作库中「{where}」公布学费如下（请与缴费通知书核对）："
        )
        for rate in shown:
            steps.append("· " + format_rate_line(rate))
    if invoice_amount is not None:
        cur = invoice_currency or (tuition.rates[0].currency if tuition.rates else "")
        steps.append(
            f"您已提供的应付金额为 {invoice_amount} {cur}；汇款金额以学校缴费通知书为准，"
            "目录价仅作对照。"
        )
    # Prefer invoice amount when currency aligns with FX quota; else catalog rates.
    compared = False
    if invoice_amount is not None and invoice_currency and quota_currency \
            and str(invoice_currency).upper() == str(quota_currency).upper() \
            and quota_amount is not None:
        try:
            from decimal import Decimal
            inv = Decimal(str(invoice_amount))
            quota = Decimal(str(quota_amount))
            if inv > quota:
                steps.append(
                    f"您提供的应付金额 {inv} {invoice_currency} 高于年度便利化额度 "
                    f"{quota} {quota_currency}，超额度需按银行要求补充学费证明等材料。"
                )
            else:
                steps.append(
                    f"您提供的应付金额 {inv} {invoice_currency} 未超过年度便利化额度 "
                    f"{quota} {quota_currency}（仍以银行审核为准）。"
                )
            compared = True
        except Exception:
            compared = False
    if not compared and tuition.rates:
        cmp = compare_to_quota(
            tuition.rates, quota_amount=quota_amount, quota_currency=quota_currency)
        if cmp:
            steps.append(cmp)
    return steps


def _deterministic_body(*, place: str, purpose_zh: str, mode: str, facts: dict,
                        materials: list[Material],
                        tuition: TuitionContext | None = None,
                        invoice_amount=None, invoice_currency: str | None = None,
                        profile_facts: dict | None = None
                        ) -> tuple[AnswerBody, dict]:
    amount = facts.get("annual_quota_amount")
    currency = facts.get("annual_quota_currency") or "USD"
    remitter = facts.get("remitter_relation") or "按规定的汇款人关系"
    over = facts.get("over_quota_allowed")
    quota_line = (
        f"中国内地个人年度便利化购汇额度约为等值 {amount} {currency}。"
        if amount is not None else
        "请按银行公示的年度便利化购汇额度办理。"
    )

    tuition_bits = _tuition_steps(
        tuition or TuitionContext(),
        invoice_amount=invoice_amount,
        invoice_currency=invoice_currency,
        quota_amount=amount,
        quota_currency=currency,
    ) if tuition else []

    if tuition and tuition.school_name and mode != "overseas" and purpose_zh == "学费":
        target = tuition.school_name
        if tuition.program_name and tuition.match in {"exact_program", "school_wide"}:
            target = f"{tuition.school_name}（{tuition.program_name}）"
        steps = [
            f"确认向{target}（{place}）支付{purpose_zh}，并核对学校收款账户与缴费通知书。",
            *tuition_bits,
            quota_line + (f"汇款人关系要求：{remitter}。" if remitter else ""),
        ]
        summary = (
            f"已结合「{tuition.school_name}」目录学费与中国内地留学购汇政策，"
            f"整理汇往{place}的{purpose_zh}准备要点。"
        )
    else:
        steps = [
            f"确认向{place}支付{purpose_zh}，并准备收款方账户信息。",
            *tuition_bits,
            quota_line + (f"汇款人关系要求：{remitter}。" if remitter else ""),
        ]
        if mode == "overseas":
            summary = f"已结合中国内地购汇政策，整理赴{place}留学相关的换汇与境外消费准备要点。"
        else:
            summary = f"已结合中国内地留学购汇政策，整理汇往{place}的{purpose_zh}准备要点。"

    enrich_payload: dict = {}
    if tuition is not None and purpose_zh == "学费" and mode != "overseas":
        pay_steps, enrich_payload = enrich_tuition_advice_steps(
            tuition, invoice_amount=invoice_amount, invoice_currency=invoice_currency,
            profile_facts=profile_facts or facts)
        steps.extend(pay_steps)

    if over:
        steps.append("若超过年度便利化额度，需按超额度要求补充学费/在读/护照签证等证明材料后办理。")
    elif over is False:
        steps.append("额度内购汇按便利化流程办理；超额度政策未在当前资料中确认，需向办理银行核实。")
    else:
        steps.append("是否允许超额购汇以办理银行当时审核要求为准。")
    required = [m.name_zh for m in materials if m.applicability == "required"][:4]
    if required:
        steps.append("常用材料包括：" + "、".join(required) + "。")
    if tuition and tuition.rates:
        steps.append("到账后保留购汇/汇款凭证，并按学校要求回传付款证明。")

    body = AnswerBody(
        summary=summary,
        steps=[s for s in steps if s],
        materials=materials,
        limitations=_limitations(mode=mode, destination=place, tuition=tuition),
    )
    return body, enrich_payload


def _claims_from_citations(citations: list[Citation], materials: list[Material]) -> list[Claim]:
    claims = []
    for citation in citations:
        mat = next((m for m in materials if m.citation_id == citation.citation_id), None)
        claims.append(Claim(
            claim_id=f"claim-{citation.citation_id.lower()}",
            text=citation.quote,
            risk="high",
            citation_ids=[citation.citation_id],
            requirement_key=mat.requirement_key if mat else None,
        ))
    return claims


def _tuition_citations(tuition: TuitionContext, *, start_index: int = 1) -> list[Citation]:
    """Attach catalog source URLs as citations (quote = published remark / rate line)."""
    from datetime import datetime, timezone
    out: list[Citation] = []
    seen: set[str] = set()
    for rate in tuition.rates:
        url = (rate.source_url or "").strip()
        if not url or url in seen:
            continue
        seen.add(url)
        cid = f"T{len(out) + start_index}"
        quote = format_rate_line(
            rate, school=tuition.school_name, program=tuition.program_name)
        out.append(Citation(
            citation_id=cid,
            record_id=tuition.program_id or tuition.school_id or "tuition-catalog",
            revision_id="tuition-catalog",
            evidence_id=f"tuition:{tuition.program_id or tuition.school_id}:{len(out)}",
            source_name=f"{tuition.school_name or '学校'} 公布学费",
            source_url=url,
            quote=quote,
            verified_at=datetime.now(timezone.utc),
            synthetic=False,
        ))
        if len(out) >= 3:
            break
    return out


class FxWorkflow:
    def __init__(self, *, model: StructuredModel | None = None,
                 policy_loader: FxPolicyLoader | None = None,
                 tuition_resolver: TuitionResolver | None = None,
                 renderer: PromptRenderer | None = None,
                 remitter_country: str = REMITTER_COUNTRY):
        self.model = model
        self.policy_loader = policy_loader or Agent1FxPolicyRead().get
        self.tuition_resolver = tuition_resolver or resolve_tuition
        self.renderer = renderer or PromptRenderer()
        self.remitter_country = remitter_country

    async def _draft_with_llm(self, *, request: OrchestrationInput, verified: AnswerBody,
                              policy_facts: dict) -> AnswerBody | None:
        if self.model is None:
            return None
        prompt = self.renderer.render(
            "write_fx_answer.jinja2",
            user_text=request.text,
            verified_json=json.dumps(policy_facts, ensure_ascii=False, sort_keys=True, default=str),
        )
        written = await self.model.parse(prompt=prompt, output_type=WrittenAnswer,
                                         operation="write_fx_answer")
        candidate = AnswerBody(
            summary=written.summary,
            steps=list(written.steps) or list(verified.steps),
            materials=verified.materials,
            limitations=verified.limitations,
        )
        validate_written_answer(candidate, verified)
        return candidate

    def _resolve_tuition_context(self, facts: dict) -> TuitionContext | None:
        school = facts.get("school")
        if not school:
            return None
        return self.tuition_resolver(
            school=str(school),
            course=(str(facts["course"]) if facts.get("course") else None),
            destination_country=(
                str(facts["destination_country"]) if facts.get("destination_country") else None),
        )

    async def run(self, task: DomainTask, request: OrchestrationInput) -> DomainResult:
        facts, basis = merged_facts(task, request)
        intent = task.intent
        operations = {action.operation for action in task.actions}
        aspects = {aspect for action in task.actions for aspect in action.aspects}
        chat_only = bool(operations) and operations <= {
            "explain", "compare", "assess", "troubleshoot", "focus"
        }
        # The policy corpus deliberately does not contain a live price feed or
        # bank-specific fee table.  Answer that boundary directly instead of
        # asking unrelated Profile questions or inventing a number.
        if "fees" in aspects and aspects <= {"fees"}:
            return DomainResult(
                task_id=task.task_id, domain="fx", status="needs_verification",
                answer=AnswerBody(
                    summary="当前资料不能给出实时汇率或确定手续费。",
                    steps=["手续费和实际到账金额取决于办理银行、中转行、收款行及当时汇率。"],
                    limitations=["请在提交汇款前向办理银行获取当次报价和费用明细。"],
                ),
                evidence_refs=["fx-capability-boundary:no-live-pricing"],
                next_actions=["向办理银行确认汇率、手续费和预计到账金额"],
                projection="chat_only", observed_facts=facts, observed_basis=basis,
                payload={"capability_boundary": "no_live_pricing"},
                model_version=MODEL_VERSION_RULES,
            )
        if intent in _PURPOSE and "remittance_purpose" not in facts:
            facts["remittance_purpose"] = _PURPOSE[intent]
            basis["remittance_purpose"] = PURPOSE_LABEL[_PURPOSE[intent]]
        if intent == "overseas_spending":
            facts["fx_mode"] = "overseas"
            basis.setdefault("fx_mode", "境外消费")
            purpose = facts.get("remittance_purpose") or "living"
        elif intent in _PURPOSE:
            purpose = facts.get("remittance_purpose") or _PURPOSE[intent]
            facts["fx_mode"] = purpose
            basis.setdefault("fx_mode", PURPOSE_LABEL.get(purpose, purpose))
        else:
            purpose = facts.get("remittance_purpose") or "tuition"
            facts["fx_mode"] = purpose

        missing = []
        if not facts.get("destination_country"):
            missing.append("destination_country")
        if intent in _PURPOSE and not facts.get("remittance_purpose"):
            missing.append("remittance_purpose")
        # Tuition remittance needs a school so advice can cite catalog fees.
        # Pure quota/limit questions stay FX-policy-only and do not require school.
        limits_only = "limits" in aspects and aspects <= {"limits"}
        if intent == "remittance_tuition" and not facts.get("school") and not limits_only:
            missing.append("school")
        if missing:
            label = {
                "remittance_tuition": "学费汇款",
                "remittance_living": "生活费汇款",
                "overseas_spending": "境外消费与换汇",
            }.get(intent, "外汇与汇款")
            return needs_input(
                task, "fx",
                summary=f"还需要一些信息才能整理{label}准备事项。",
                missing=missing, facts=facts, basis=basis,
                profile_version=request.profile_version,
            )

        country = facts["destination_country"]
        place = COUNTRY_LABEL.get(country, country)
        mode = "overseas" if intent == "overseas_spending" else purpose
        purpose_code = purpose if purpose in PURPOSE_LABEL else "tuition"

        tuition: TuitionContext | None = None
        if intent == "remittance_tuition":
            tuition = self._resolve_tuition_context(facts)
            if tuition and tuition.match == "ambiguous_school":
                return needs_input(
                    task, "fx",
                    summary="匹配到多所学校，请确认学校全称后再整理学费汇款要点。",
                    missing=["school"], facts=facts, basis=basis,
                    profile_version=request.profile_version,
                )
            if tuition and tuition.match == "ambiguous_program":
                return needs_input(
                    task, "fx",
                    summary=(tuition.note
                             or "该校学费因项目而异，请补充或确认课程/学位名称。"),
                    missing=["course"], facts=facts, basis=basis,
                    profile_version=request.profile_version,
                )

        try:
            bundle: FxPolicyBundle | None = self.policy_loader(
                purpose_code=purpose_code, remitter_country=self.remitter_country)
        except TypeError:
            # Allow zero-arg test loaders that already close over purpose.
            bundle = self.policy_loader()  # type: ignore[call-arg]
        except Exception:
            bundle = None

        if bundle is None:
            return DomainResult(
                task_id=task.task_id,
                domain="fx",
                status="cannot_answer",
                answer=AnswerBody(
                    summary="暂时读不到可用的留学购汇政策资料，不能给出带出处的汇款建议。",
                    limitations=["外汇政策尚未发布或本地工作库不可用，请稍后重试。"],
                ),
                observed_facts=facts,
                observed_basis=basis,
                notices=[Notice(code="fx_policy_missing",
                                message="Agent1 FX_POLICY 工作表无匹配记录。")],
                model_version=MODEL_VERSION_RULES,
            )

        citations, materials, quotes, policy_facts = build_fx_answer_parts(
            bundle, destination_label=place, mode=mode)
        if not citations:
            return DomainResult(
                task_id=task.task_id,
                domain="fx",
                status="needs_verification",
                answer=AnswerBody(
                    summary="已找到留学购汇政策条目，但缺少可引用的原文摘录，暂不能给出确定建议。",
                    limitations=["需补充 FX_EVIDENCE 原文后再回答。"],
                ),
                observed_facts=facts,
                observed_basis=basis,
                evidence_refs=[bundle.policy_id],
                payload={"policy_id": bundle.policy_id},
                model_version=MODEL_VERSION_RULES,
            )

        if tuition is not None:
            policy_facts["tuition"] = tuition.as_dict()
            if facts.get("amount") is not None:
                policy_facts["invoice_amount"] = facts.get("amount")
                policy_facts["invoice_currency"] = facts.get("currency")
            citations = list(citations) + _tuition_citations(tuition)

        purpose_zh = PURPOSE_LABEL.get(purpose_code, purpose_code)
        verified, enrich_payload = _deterministic_body(
            place=place, purpose_zh=purpose_zh, mode=mode,
            facts=policy_facts, materials=materials,
            tuition=tuition,
            invoice_amount=facts.get("amount"),
            invoice_currency=facts.get("currency"),
            profile_facts=facts,
        )
        if tuition is not None:
            policy_facts["payment"] = enrich_payload.get("payment")
            policy_facts["card_advice"] = enrich_payload.get("card_advice")
        if "limits" in aspects and aspects <= {"limits"}:
            quota_steps = [step for step in verified.steps
                           if "额度" in step or "超额" in step]
            verified = verified.model_copy(update={
                "summary": "关于留学购汇额度，当前政策摘录给出的要点如下。",
                "steps": quota_steps,
                "materials": [],
            })
            chat_only = True
        body = verified
        model_version = MODEL_VERSION_RULES
        notices: list[Notice] = []
        try:
            drafted = await self._draft_with_llm(
                request=request, verified=verified, policy_facts=policy_facts)
            if drafted is not None:
                body = drafted
                model_version = f"{self.model.model_name}+{PROMPT_VERSION}"
        except Exception:
            notices.append(Notice(
                code="fx_llm_fallback",
                message="模型未能完成本轮外汇表述，已使用政策摘录与确定性要点。",
            ))

        claims = _claims_from_citations(citations, materials)
        next_actions = [
            f"核对汇往{place}的{purpose_zh}收款信息",
            "按额度内/超额准备对应材料并保留购汇凭证",
        ]
        if tuition and tuition.school_name:
            next_actions.insert(
                0, f"核对{tuition.school_name}缴费通知书金额与收款账户")
        if enrich_payload.get("card_advice", {}).get("recommendations"):
            next_actions.append("若走可刷卡/多币种通道，按测算核对办卡与缴费路径")
        return DomainResult(
            task_id=task.task_id,
            domain="fx",
            status="answered",
            answer=body,
            claims=claims,
            citations=citations,
            evidence_refs=[bundle.policy_id, *[c.evidence_id for c in citations[:6]]],
            next_actions=next_actions,
            notices=notices,
            projection="chat_only" if chat_only else "advice",
            observed_facts=facts,
            observed_basis=basis,
            payload={
                "fx_mode": mode,
                "destination_country": country,
                "remittance_purpose": purpose_code,
                "policy_id": bundle.policy_id,
                "policy_title": bundle.title,
                "quotes": quotes,
                "materials": [m.model_dump(mode="json") for m in materials],
                "citations": [c.model_dump(mode="json") for c in citations],
                "capabilities": [action.model_dump(mode="json") for action in task.actions],
                "tuition": tuition.as_dict() if tuition else None,
                "payment": enrich_payload.get("payment"),
                "card_advice": enrich_payload.get("card_advice"),
            },
            knowledge_read_id=f"agent1-fx-rds:{bundle.environment}",
            model_version=model_version,
        )
