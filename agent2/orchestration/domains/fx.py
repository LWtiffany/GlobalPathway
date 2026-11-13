"""FX / remittance / overseas-spend workflow grounded on Agent1 FX policy quotes.

Tuition remittance answers also pull SCHOOL/PROGRAM/TUITION from RDS when the
profile names a school (and optionally a course) — amounts are never hardcoded.
"""

from __future__ import annotations

import json
import re
from dataclasses import replace

from agent2.fx_knowledge import (Agent1FxPolicyRead, FxPolicyBundle, FxPolicyLoader,
                                 PURPOSE_LABEL, build_fx_answer_parts)
from agent2.llm.contracts import StructuredModel
from agent2.llm.prompts import PromptRenderer
from agent2.llm.schemas import WrittenAnswer
from agent2.llm.validation import validate_written_answer
from agent2.models import (AnswerBody, Claim, Citation, FollowUpQuestion, Material,
                           Notice)
from agent2.tuition_knowledge import (TuitionContext, TuitionResolver,
                                      compare_to_quota, fee_status_tiers,
                                      filter_rates_by_fee_status, format_rate_line,
                                      resolve_tuition)

from ..contracts import DomainResult, DomainTask, OrchestrationInput
from .minimal import COUNTRY_LABEL, merged_facts, needs_input

PROMPT_VERSION = "write-fx-answer-v2"
MODEL_VERSION_RULES = "fx-policy-rules-3"
REMITTER_COUNTRY = "CN"

_PURPOSE = {
    "remittance_tuition": "tuition",
    "remittance_living": "living",
}

_LIVE_PRICING = re.compile(
    r"汇率|牌价|实时|手续费|到账(?:多少|金额)|哪家.{0,8}便宜|最便宜|何时.{0,6}最低", re.I)
_CHEAPEST_BANK = re.compile(r"哪家.{0,10}(?:便宜|划算|最低)|最便宜|汇率最低", re.I)
_NON_RESIDENT = re.compile(r"非居民|不是内地|境外身份|香港居民购汇|外籍", re.I)
_NON_STUDY = re.compile(r"投资|炒股|买房|移民结汇|贸易货款", re.I)
_HAS_INVOICE = re.compile(r"已有(?:缴费)?通知|已经有.{0,6}(?:账单|通知)|缴费通知书", re.I)
_PROXY = re.compile(r"父母|家长|代汇|代办|谁.{0,4}汇|谁更合适", re.I)
_BENEFICIARY = re.compile(r"收款(?:人|信息|账户|账号)|SWIFT|汇款信息|要哪些字段", re.I)
_URGENCY = re.compile(r"截止|来不及|赶紧|先做|先什么|快到了", re.I)
_LIVING_SAFE = re.compile(r"安全|孩子|父母在内地|我在内地", re.I)

BENEFICIARY_FIELDS = (
    "收款人名称（与学校账单一致）",
    "收款账号",
    "开户银行名称",
    "SWIFT/BIC（如适用）",
    "汇款币种与金额",
    "备注/附言中的学生姓名、学号或申请号（如账单要求）",
)


def _fx_limitations(*, mode: str, destination: str) -> list[str]:
    notes = [
        "依据为中国内地个人留学购汇政策摘录，办理以银行/外汇局当时规则为准。",
        "不含实时汇率、手续费或可成交报价；不保证汇出或超额申请获批。",
        f"收款地按{destination}整理；若汇款人非中国内地居民，规则可能不同，建议转人工核对。",
    ]
    if mode == "overseas":
        notes.append("境外消费另受发卡行与当地支付网络规则约束，本回答只覆盖购汇额度与用途材料要点。")
    return notes


def _filter_materials(materials: list[Material], *, by_proxy: bool = False,
                      has_invoice: bool = False) -> list[Material]:
    """Keep relevant materials; drop invoice proof when user already has it."""
    out: list[Material] = []
    for mat in materials:
        cond = (mat.condition_text or "")
        code = mat.document_code or ""
        if has_invoice and code in {"tuition_fee_proof"}:
            continue
        if by_proxy:
            out.append(mat)
            continue
        if "由他人代办" in cond or "代办" in cond:
            # Keep proxy docs only when proxy path is active.
            continue
        out.append(mat)
    return out


def _beneficiary_steps(*, tuition: TuitionContext | None = None) -> list[str]:
    steps = [
        "向学校索取或核对缴费通知上的收款信息时，至少确认："
        + "、".join(BENEFICIARY_FIELDS) + "。",
        "不要自行编造 SWIFT/账号；以学校官方缴费通知或门户显示为准。",
    ]
    payment = tuition.payment if tuition else None
    if payment and (payment.payee or payment.portal_name or payment.portal_url):
        from agent2.tuition_payment_advice import PORTAL_NAME_ZH, _zh_payee
        bits = []
        if payment.payee:
            payee_zh = _zh_payee(payment.payee)
            # English-only payee lines stay payload-only rather than a filler.
            if payee_zh:
                bits.append(f"收款方资料显示为 {payee_zh}")
        if payment.portal_name or payment.portal_url:
            portal = PORTAL_NAME_ZH.get((payment.portal_name or "").strip(), "") \
                or "学校缴费门户"
            bits.append(
                f"缴费入口：{portal}"
                + (f"（{payment.portal_url}）" if payment.portal_url else ""))
        steps.append("；".join(bits) + "。仍请与最新账单逐项核对。")
    return steps


def _payment_limitations(tuition: TuitionContext | None) -> list[str]:
    notes: list[str] = []
    if not tuition:
        return notes
    if tuition.rates:
        urls = sorted({r.source_url for r in tuition.rates if r.source_url})
        if urls:
            notes.append("参考来源：" + "；".join(urls[:3]))
    if tuition.note and "未找到" not in tuition.note and "工作库" not in tuition.note:
        # Keep useful programme cautions; drop agent/meta wording.
        notes.append(tuition.note)
    return notes


def _tuition_reference_steps(tuition: TuitionContext, *, invoice_amount,
                             invoice_currency: str | None) -> list[str]:
    """School-facing tuition figures and invoice check — no FX/quota wording."""
    steps: list[str] = []
    if tuition.match in {"ambiguous_school", "ambiguous_program", "not_found", "none"}:
        return steps
    if tuition.school_name and tuition.rates:
        school = tuition.school_display or tuition.school_name
        where = school
        if tuition.program_name:
            where = f"{school}（{tuition.program_name}）"
        years = [r.academic_year for r in tuition.rates if r.academic_year]
        focus_year = years[0] if years else None
        shown = [r for r in tuition.rates
                 if focus_year is None or r.academic_year == focus_year][:6]
        # Keep rates in the same step so the UI does not number each fee line.
        rates_txt = "；".join(format_rate_line(rate) for rate in shown)
        steps.append(f"{where} 学费参考（请与缴费通知核对）：{rates_txt}")
    if invoice_amount is not None:
        from agent2.tuition_knowledge import _format_money
        cur = invoice_currency or (tuition.rates[0].currency if tuition.rates else "")
        steps.append(
            f"您提供的应付金额为 {_format_money(invoice_amount, cur)}；"
            "实际汇出请以缴费通知书为准。"
        )
    return steps


def _quota_steps(*, tuition: TuitionContext | None, invoice_amount, invoice_currency,
                 quota_amount, quota_currency: str | None) -> list[str]:
    steps: list[str] = []
    compared = False
    if invoice_amount is not None and invoice_currency and quota_currency \
            and str(invoice_currency).upper() == str(quota_currency).upper() \
            and quota_amount is not None:
        try:
            from decimal import Decimal
            from agent2.tuition_knowledge import _format_money
            inv = Decimal(str(invoice_amount))
            quota = Decimal(str(quota_amount))
            inv_txt = _format_money(inv, invoice_currency)
            quota_txt = _format_money(quota, quota_currency)
            if inv > quota:
                steps.append(
                    f"应付金额约 {inv_txt}，高于年度便利化额度约 {quota_txt}；"
                    "超额度请按银行要求补充学费证明等材料。"
                )
            else:
                steps.append(
                    f"应付金额约 {inv_txt}，未超过年度便利化额度约 {quota_txt}"
                    "（仍以银行审核为准）。"
                )
            compared = True
        except Exception:
            compared = False
    if not compared and tuition and tuition.rates:
        cmp = compare_to_quota(
            tuition.rates, quota_amount=quota_amount, quota_currency=quota_currency)
        if cmp:
            steps.append(cmp)
    return steps


def _school_payment_body(*, place: str, tuition: TuitionContext,
                         invoice_amount=None, invoice_currency: str | None = None,
                         profile_facts: dict | None = None
                         ) -> tuple[list[dict], dict]:
    """School billing bubble + optional separate card-advice bubble."""
    if not tuition.school_name:
        return [], {}
    target = tuition.school_display or tuition.school_name
    if tuition.program_name and tuition.match in {"exact_program", "school_wide", "school_only"}:
        target = f"{target}（{tuition.program_name}）"
    steps = [
        *_tuition_reference_steps(
            tuition, invoice_amount=invoice_amount, invoice_currency=invoice_currency),
    ]
    from agent2.tuition_payment_advice import enrich_tuition_advice_steps
    pay_steps, enrich_payload = enrich_tuition_advice_steps(
        tuition, invoice_amount=invoice_amount, invoice_currency=invoice_currency,
        profile_facts=profile_facts or {})
    steps.extend(pay_steps)
    companions = [{
        "kind": "school_payment",
        "summary": f"缴{target}学费：按学校账单与官方缴费入口办理。",
        "steps": [s for s in steps if s],
        "limitations": _payment_limitations(tuition),
    }]
    cards = enrich_payload.get("card_advice") or {}
    card_lines = list(cards.get("steps") or [])
    if card_lines:
        card_limits = list(cards.get("limitations") or [])
        companions.append({
            "kind": "card_advice",
            "summary": cards.get("reason") or "可参考的卡片",
            "steps": card_lines,
            "limitations": card_limits,
        })
    return companions, enrich_payload


def _fx_remittance_body(*, place: str, purpose_zh: str, mode: str, facts: dict,
                        materials: list[Material],
                        tuition: TuitionContext | None = None,
                        invoice_amount=None, invoice_currency: str | None = None,
                        aspects: set[str] | None = None, user_text: str = "",
                        has_invoice: bool = False, by_proxy: bool = False,
                        want_beneficiary: bool = False,
                        ) -> AnswerBody:
    """China mainland FX / remittance action items only."""
    aspects = aspects or set()
    amount = facts.get("annual_quota_amount")
    currency = facts.get("annual_quota_currency") or "USD"
    remitter = facts.get("remitter_relation")
    over = facts.get("over_quota_allowed")
    family_quota = facts.get("can_use_family_quota")
    direct = facts.get("tuition_direct_to_school")
    from agent2.tuition_knowledge import _format_money
    if amount is not None:
        quota_line = f"中国内地个人年度便利化购汇额度约为等值 {_format_money(amount, currency)}。"
    else:
        quota_line = "请按银行公示的年度便利化购汇额度办理。"

    steps: list[str] = []
    deadline = facts.get("payment_deadline")
    if deadline and purpose_zh == "学费" and mode != "overseas":
        from agent2.tuition_timing import timing_step
        steps.append(timing_step(deadline)
                     or f"学校缴费截止日按您提供的信息为 {deadline}；请按此倒排购汇与汇出时间。")
    if want_beneficiary or _BENEFICIARY.search(user_text or ""):
        steps.extend(_beneficiary_steps(tuition=tuition))

    if _URGENCY.search(user_text or "") or "timeline" in aspects:
        steps.extend([
            "截止日期临近时，建议按此顺序推进：",
            "1）核对最新缴费通知金额与收款信息（名称/账号/银行/SWIFT/币种）。",
            "2）确认本人或代办人额度是否足够，并预约网银/柜台办理窗口。",
            "3）若可能超额，同步准备学费/在读/护照签证等超额证明，避免临柜缺件。",
        ])

    if mode == "overseas":
        steps.extend([
            f"赴{place}留学相关的境外消费/购汇：先了解个人年度便利化额度与用途申报要求。",
            quota_line,
            "境外刷卡、取现是否占用额度及清算币种，以发卡行与支付网络规则为准；本服务不提供实时牌价。",
            "出发前可准备身份证件，并按银行要求确认经常项目用途材料；保留消费与购汇凭证。",
        ])
    else:
        steps.append(
            f"从中国内地购汇并汇往{place}支付{purpose_zh}时，先确认汇款用途与收款信息。")
        steps.append(quota_line + (f"汇款人关系要求：{remitter}。" if remitter else ""))
        if family_quota:
            steps.append("资料显示近亲属相关办理路径存在；是否可用家庭成员额度以银行审核为准，不能保证获批。")
        if by_proxy or _PROXY.search(user_text or ""):
            steps.append(
                "父母等近亲属代汇/代办时，通常需委托人与受托人身份证件、授权委托书及亲属关系证明；"
                "本人办理与直系亲属代办都常见，选谁更合适取决于额度占用、材料是否齐全与银行受理习惯，"
                "本服务不承诺某一种路径一定更快获批。")
        if purpose_zh == "学费" and direct:
            steps.append("学费宜按学校账单直付学校指定收款账户，并保留付款凭证。")
        if purpose_zh == "生活费" and _LIVING_SAFE.search(user_text or ""):
            steps.append(
                "家长在内地、学生在境外时：学费优先直付学校；生活费汇入学生本人当地账户，"
                "小额分次并保留凭证；避免向不明个人账户大额转账。")
        steps.extend(_quota_steps(
            tuition=tuition, invoice_amount=invoice_amount,
            invoice_currency=invoice_currency,
            quota_amount=amount, quota_currency=currency))
        if over:
            steps.append("折算后如超过年度便利化额度，按超额度要求补充学费/在读/护照签证等证明后再办理；是否获批以银行审核为准。")
        elif over is False:
            steps.append("额度内购汇按便利化流程办理；超额度政策未在当前资料中确认，请向办理银行核实。")
        else:
            steps.append("是否允许超额购汇以办理银行当时审核要求为准。")

    if has_invoice:
        steps.append("您已有缴费通知书时，学费证明可视为已具备；仍需核对这些材料是否还缺：身份证件、护照签证、代办时的亲属与授权文件等。")

    steps.append("办妥后保留购汇/汇款凭证，便于学校核验与后续查账。")

    shown_materials = _filter_materials(
        materials, by_proxy=by_proxy or bool(_PROXY.search(user_text or "")),
        has_invoice=has_invoice)

    if tuition and tuition.school_name and purpose_zh == "学费" and mode != "overseas":
        summary = f"汇往{place}支付{tuition.school_display or tuition.school_name}学费：购汇额度、材料与超额办理要点。"
    elif mode == "overseas":
        summary = f"赴{place}留学相关的购汇与境外消费准备要点。"
    elif want_beneficiary:
        summary = f"汇往{place}的{purpose_zh}：先核对学校收款信息字段。"
    else:
        summary = f"汇往{place}的{purpose_zh}：购汇额度与材料准备要点。"

    return AnswerBody(
        summary=summary,
        steps=[s for s in steps if s],
        materials=shown_materials,
        limitations=_fx_limitations(mode=mode, destination=place),
    )


def _deterministic_body(*, place: str, purpose_zh: str, mode: str, facts: dict,
                        materials: list[Material],
                        tuition: TuitionContext | None = None,
                        invoice_amount=None, invoice_currency: str | None = None,
                        profile_facts: dict | None = None,
                        aspects: set[str] | None = None, user_text: str = "",
                        ) -> tuple[AnswerBody, dict]:
    """Return FX remittance body + payload (may include a school-payment companion bubble)."""
    enrich_payload: dict = {}
    companions: list[dict] = []
    text = user_text or ""
    profile = profile_facts or {}
    has_invoice = bool(_HAS_INVOICE.search(text))
    payer = profile.get("payer") or facts.get("payer")
    by_proxy = bool(_PROXY.search(text) or payer in {
        "parent", "parents", "family", "proxy"})
    want_beneficiary = bool(_BENEFICIARY.search(text))
    # Carry deadline/payer into FX body facts for wording.
    merged_facts_for_body = {
        **facts,
        **({k: profile[k] for k in ("payment_deadline", "payer") if profile.get(k)}),
    }
    if tuition is not None and purpose_zh == "学费" and mode != "overseas":
        payment_companions, enrich_payload = _school_payment_body(
            place=place, tuition=tuition,
            invoice_amount=invoice_amount, invoice_currency=invoice_currency,
            profile_facts=profile)
        companions.extend(payment_companions)

    fx_body = _fx_remittance_body(
        place=place, purpose_zh=purpose_zh, mode=mode, facts=merged_facts_for_body,
        materials=materials, tuition=tuition,
        invoice_amount=invoice_amount, invoice_currency=invoice_currency,
        aspects=aspects, user_text=text, has_invoice=has_invoice,
        by_proxy=by_proxy, want_beneficiary=want_beneficiary)
    enrich_payload["companion_bubbles"] = companions
    return fx_body, enrich_payload


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
            rate, school=tuition.school_display or tuition.school_name,
            program=tuition.program_name)
        out.append(Citation(
            citation_id=cid,
            record_id=tuition.program_id or tuition.school_id or "tuition-catalog",
            revision_id="tuition-catalog",
            evidence_id=f"tuition:{tuition.program_id or tuition.school_id}:{len(out)}",
            source_name=f"{tuition.school_display or tuition.school_name or '学校'} 公布学费",
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
        user_text = request.text or ""
        chat_only = bool(operations) and operations <= {
            "explain", "compare", "assess", "troubleshoot", "focus"
        }
        # Capability boundaries before Profile chasing.
        if _CHEAPEST_BANK.search(user_text) or (
                "fees" in aspects and re.search(r"便宜|划算|最低", user_text)):
            return DomainResult(
                task_id=task.task_id, domain="fx", status="answered",
                answer=AnswerBody(
                    summary="不能比较哪家银行购汇/汇款最便宜，也无法判断何时汇率最低。",
                    steps=[
                        "本服务不提供银行比价或择时建议。",
                        "请向拟办理银行询价，并按其留学购汇/汇出材料要求办理。",
                    ],
                    limitations=["不含实时汇率、手续费或可成交报价。"],
                ),
                evidence_refs=["fx-capability-boundary:no-bank-comparison"],
                next_actions=["向办理银行获取当次汇率与费用报价"],
                projection="chat_only", observed_facts=facts, observed_basis=basis,
                payload={"capability_boundary": "no_bank_comparison"},
                model_version=MODEL_VERSION_RULES,
            )
        if ("fees" in aspects and aspects <= {"fees"}) or (
                _LIVE_PRICING.search(user_text)
                and not re.search(r"学费|生活费|材料|额度|超额|收款", user_text)):
            return DomainResult(
                task_id=task.task_id, domain="fx", status="needs_verification",
                answer=AnswerBody(
                    summary="当前资料不能给出实时汇率、确定手续费或实际到账金额。",
                    steps=["手续费和实际到账金额取决于办理银行、中转行、收款行及当时汇率。"],
                    limitations=["请在提交汇款前向办理银行获取当次报价和费用明细。"],
                ),
                evidence_refs=["fx-capability-boundary:no-live-pricing"],
                next_actions=["向办理银行确认汇率、手续费和预计到账金额"],
                projection="chat_only", observed_facts=facts, observed_basis=basis,
                payload={"capability_boundary": "no_live_pricing"},
                model_version=MODEL_VERSION_RULES,
            )
        if _NON_RESIDENT.search(user_text) or _NON_STUDY.search(user_text):
            return DomainResult(
                task_id=task.task_id, domain="fx", status="answered",
                answer=AnswerBody(
                    summary="您描述的情形可能不适用中国内地居民留学购汇规则。",
                    steps=[
                        "非内地居民身份，或非留学经常项目用途时，适用规则不同。",
                        "建议转人工/办理银行按实际身份与用途核实，勿直接套用留学便利化路径。",
                    ],
                    limitations=["本模块仅覆盖内地居民留学相关购汇/汇出/境外消费准备要点。"],
                ),
                evidence_refs=["fx-capability-boundary:out-of-scope-identity-or-purpose"],
                next_actions=["联系办理银行或人工顾问核对适用规则"],
                projection="chat_only", observed_facts=facts, observed_basis=basis,
                payload={"capability_boundary": "non_study_or_non_resident"},
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
        limits_only = "limits" in aspects and not (aspects - {"limits", "process", "documents"})
        beneficiary_only = bool(_BENEFICIARY.search(user_text)) and not facts.get("school")
        # Quota / over-quota / beneficiary-field FAQs can answer without school.
        skip_school = limits_only or beneficiary_only or bool(
            re.search(r"购汇额度|超额|便利化|年度额度|超年度", user_text))
        # Infer payer from this turn when user already said 父母/本人.
        if not facts.get("payer") and _PROXY.search(user_text):
            facts["payer"] = "parent"
            basis.setdefault("payer", "代汇/父母语境")
        elif not facts.get("payer") and re.search(r"本人|自己(?:汇|付|交)|我来(?:汇|付|交)", user_text):
            facts["payer"] = "self"
            basis.setdefault("payer", "本人汇款语境")
        if not facts.get("destination_country") and not skip_school:
            missing.append("destination_country")
        if intent in _PURPOSE and not facts.get("remittance_purpose"):
            missing.append("remittance_purpose")
        # Tuition remittance needs a school so advice can cite catalog fees.
        if (intent == "remittance_tuition" and not facts.get("school")
                and not skip_school and not _HAS_INVOICE.search(user_text)):
            missing.append("school")
        # Align with clarify CONDITION_TABLE: ask deadline after destination/school.
        if (intent == "remittance_tuition" and not facts.get("payment_deadline")
                and not skip_school and not limits_only):
            missing.append("payment_deadline")
        # Living remittance: always confirm who remits when not already known.
        if intent == "remittance_living" and not facts.get("payer") and not limits_only:
            missing.append("payer")
        # Default destination for pure quota FAQs when unspecified.
        if skip_school and not facts.get("destination_country"):
            facts["destination_country"] = "US"
            basis.setdefault("destination_country", "额度咨询默认目的地占位")
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
            # Split fee tiers (local vs non-local) → ask once, then show only
            # the user's tier instead of both catalogue rates.
            if (intent == "remittance_tuition" and tuition is not None
                    and not facts.get("fee_status")):
                tiers = fee_status_tiers(tuition.rates)
                if tiers:
                    return DomainResult(
                        task_id=task.task_id, domain="fx", status="needs_input",
                        answer=AnswerBody(
                            summary="学校学费按本地生/非本地生分档，先确认您按哪一档缴纳。",
                            steps=["确认档位后，只按对应学费展示并测算额度与材料。"],
                        ),
                        missing_fields=["fee_status"],
                        follow_up_questions=[FollowUpQuestion(
                            question_code="ask_fee_status",
                            field="fee_status",
                            text="学校学费按本地生/非本地生分档，您按哪一档缴纳？",
                            rule="tuition-fee-tier-v1",
                            profile_version=request.profile_version,
                            input_type="single_select",
                            options=[
                                {"value": t["value"],
                                 "label": f"{t['label']}（{t['sample']}）"}
                                for t in tiers
                            ],
                        )],
                        observed_facts=facts, observed_basis=basis,
                    )
            if tuition is not None and facts.get("fee_status"):
                tuition = replace(
                    tuition,
                    rates=filter_rates_by_fee_status(
                        tuition.rates, str(facts["fee_status"])))

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
        # Keep deadline/payer from the conversation on the FX body.
        for key in ("payment_deadline", "payer"):
            if facts.get(key) is not None:
                policy_facts[key] = facts[key]
        verified, enrich_payload = _deterministic_body(
            place=place, purpose_zh=purpose_zh, mode=mode,
            facts=policy_facts, materials=materials,
            tuition=tuition,
            invoice_amount=facts.get("amount"),
            invoice_currency=facts.get("currency"),
            profile_facts=facts,
            aspects=aspects,
            user_text=user_text,
        )
        if tuition is not None:
            policy_facts["payment"] = enrich_payload.get("payment")
            policy_facts["card_advice"] = enrich_payload.get("card_advice")
            # LLM rewrite is for the FX bubble only; keep school-payment wording deterministic.
            policy_facts.pop("tuition", None)
        if limits_only:
            quota_steps = [step for step in verified.steps
                           if "额度" in step or "超额" in step or "便利化" in step]
            if not quota_steps:
                quota_steps = verified.steps[:3]
            verified = verified.model_copy(update={
                "summary": "关于留学购汇额度，当前政策摘录给出的要点如下。",
                "steps": quota_steps,
                "materials": [],
            })
            chat_only = True
        if beneficiary_only and not tuition:
            verified = verified.model_copy(update={
                "summary": "汇款前请先核对学校收款信息字段。",
                "steps": _beneficiary_steps(tuition=tuition) + [
                    s for s in verified.steps
                    if s not in _beneficiary_steps(tuition=tuition)
                ][:4],
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
            # Silent deterministic fallback — do not surface model failure copy.
            pass

        claims = _claims_from_citations(citations, materials)
        next_actions = [
            f"核对汇往{place}的{purpose_zh}收款信息",
            "按额度内/超额准备对应材料并保留购汇凭证",
        ]
        if tuition and tuition.school_name:
            next_actions.insert(
                0, f"核对{tuition.school_display or tuition.school_name}缴费通知书金额与收款账户")
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
                "companion_bubbles": enrich_payload.get("companion_bubbles") or [],
            },
            knowledge_read_id=f"agent1-fx-rds:{bundle.environment}",
            model_version=model_version,
        )
