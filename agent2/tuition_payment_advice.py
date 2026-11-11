"""Compose tuition payment + multi-currency + card-value steps for remittance advice.

Uses TUITION_PAYMENT_DETAIL and CREDIT_CARD catalog only — no invented FX/school fees.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from agent2.orchestration.domains.credit_card_engine import recommend
from agent2.orchestration.domains.credit_card_repository import (
    CreditCardRepositoryError, RdsCreditCardRepository)
from agent2.tuition_knowledge import (SCHOOL_NAME_ZH, PaymentDetail,
                                      TuitionContext, TuitionRate)

BILLING_CYCLE_ZH = {
    "per_year_advance": "按学年预付（通常一次付清全年）",
    "per_term": "按学期/学期账单",
    "per_quarter": "按学季账单",
    "monthly": "按月账单",
    "installment": "分期缴纳",
    "other": "按学校/项目另行规定",
}

INSTALLMENT_PERIOD_ZH = {
    "month": "按月",
    "term": "按学期",
    "year": "按学年",
}

# Keep card-advice steps Chinese; engine scenario ids stay English in payloads.
SCENARIO_LABELS_ZH = {
    "online": "网购", "dining": "餐饮", "travel": "旅行",
    "travel_and_transport": "旅行与交通", "travel_and_dining": "旅行与餐饮",
    "overseas": "境外消费", "mainland_china": "内地消费",
    "mainland_china_and_macau": "内地及澳门消费",
    "retail": "零售消费", "groceries": "日常采购", "general": "日常消费",
    "selected_retail": "指定零售类别", "selected_lifestyle": "指定生活消费类别",
    "dining_and_lifestyle": "餐饮及生活消费", "gas_and_groceries": "加油及日常采购",
    "tuition": "学费",
}

# Catalogue payment-method ids → Chinese labels; unmapped ids fall back to a
# generic Chinese phrase so snake_case identifiers never reach the answer.
PAYMENT_METHOD_ZH = {
    "529": "529 教育储蓄计划",
    "ach_epayment": "ACH 电子支付",
    "atm": "ATM",
    "atm_bill": "ATM 账单缴费",
    "axess_echeck": "Axess 电子支票",
    "bank_counter": "银行柜台",
    "bank_counter_atm": "银行柜台/ATM",
    "bank_draft": "银行汇票",
    "bank_remittance_wire": "银行电汇",
    "cash_in_office": "校内现金缴费",
    "check_usd": "美元支票",
    "check_usd_us_bank": "美元支票（美国银行账户）",
    "cheque_deposit_machine": "支票存款机",
    "college_arrangements": "按学院安排缴费",
    "cuhk_fee_payment_channels": "中大官方缴费渠道",
    "cusis_online": "CUSIS 在线缴费",
    "department_direct": "院系直接收款",
    "flywire_bank_transfer": "Flywire 银行转账",
    "flywire_card_option": "Flywire 卡支付选项",
    "flywire_local_currency_options": "Flywire 本地币选项",
    "flywire_overseas": "Flywire 境外通道",
    "flywire_portal": "Flywire 平台",
    "flywire_via_college": "经学院办理的 Flywire",
    "hang_seng_counter": "恒生银行柜台",
    "hku_portal_instructions": "港大门户站缴费指引",
    "hkust_registry_payment_channels": "科大教务处缴费渠道",
    "internet_banking": "网上银行",
    "internet_banking_bill": "网上银行账单缴费",
    "mitpay_ach": "MITPay ACH 缴费",
    "monthly_payment_plan": "按月分期计划",
    "online_bank": "网上银行",
    "pay_my_tuition": "PayMyTuition 平台",
    "payroll_deduction": "工资/津贴代扣",
    "pps": "缴费灵（PPS）",
    "programme_catalog_channels": "课程目录列明渠道",
    "programme_page_channels": "课程页面列明渠道",
    "programme_specific": "按课程规定",
    "sis_online": "SIS 在线缴费",
    "student_invoice_banking": "学生账单银行缴费",
    "telegraphic_transfer_hsbc_hk": "汇丰香港电汇",
    "telegraphic_transfer_nonlocal": "非本地电汇",
    "tigerhub_electronic": "TigerHub 电子缴费",
    "wechat_pay_alipay": "微信支付/支付宝",
    "wechat_pay_alipay_alipayhk": "微信支付/支付宝（含 AlipayHK）",
    "wechat_pay_alipay_fps_bocpay_unionpay": "微信支付/支付宝/转数快/银联等本地渠道",
    "wire_bank": "银行电汇",
    "wire_convera": "Convera 电汇",
    "wire_flywire": "Flywire 电汇",
    "wire_transfermate": "TransferMate 电汇",
    "yale_payment_plan": "耶鲁分期缴费计划",
    "yalepay_ach": "YalePay ACH 缴费",
}

# Catalogue portal names → Chinese labels (system names like CUSIS stay as-is).
PORTAL_NAME_ZH = {
    "HKUST taught PG programme catalog": "科大修课式研究生课程目录",
    "CUHK Graduate School Fees": "中大研究院费用页",
    "MITPay": "MITPay",
    "my.harvard / Student Accounts": "my.harvard 学生账户",
    "HKUST Registry tuition rates": "科大教务处学费页",
    "HKU SIS / student invoice": "港大 SIS 学生账单",
    "Harvard Student Financial Services": "哈佛学生财务服务",
    "CUSIS / CUHK Finance Office": "中大 CUSIS/财务处",
    "Portico (MyMoney) / Flywire": "Portico（MyMoney）/ Flywire",
    "College admissions / college finance": "学院招生/财务办公室",
    "HKUST Registry research PG tuition": "科大教务处研究型研究生学费页",
    "Student Financial Center (TigerHub)": "学生财务中心（TigerHub）",
    "Individual TPg programme pages": "各修课式研究生课程页面",
    "Saïd Business School / department": "赛德商学院/所在院系",
    "Axess Bill & Payment System": "Axess 账单与缴费系统",
    "SIS invoice / HKU Finance Office": "港大 SIS 账单/财务处",
    "Department for Continuing Education": "继续教育系",
    "YalePay": "YalePay",
    "SIS / HKUST Central Student Financials Unit": "科大 SIS/中央学生财务组",
    "College finance (varies by college)": "各学院财务处（因学院而异）",
    "Student Financial Center": "学生财务中心",
    "HKU SIS / programme fee notice": "港大 SIS/课程费用通知",
    "CUHK RES tuition pages": "中大教务处学费页",
}

CHANNEL_ZH = {
    "flywire": "Flywire",
    "convera": "Convera",
    "transfermate": "TransferMate",
    "pay_my_tuition": "PayMyTuition",
    "fps": "转数快（FPS）",
    "alipay": "支付宝",
    "wechat_pay": "微信支付",
    "unionpay_app": "银联 App 支付",
    "boc_pay": "中银智慧付（BoC Pay）",
}

_SCHOOL_NAME_PAIRS = sorted(SCHOOL_NAME_ZH.items(), key=lambda kv: -len(kv[0]))


def _has_cjk(text: str) -> bool:
    return any("\u4e00" <= ch <= "\u9fff" for ch in text)


def _zh_payee(payee: str) -> str | None:
    """Localise school names inside the payee line; keep bank details intact.

    Returns None when the result still has no Chinese content — callers then
    drop the English line and rely on a generic Chinese sentence instead.
    """
    text = payee
    for en, zh in _SCHOOL_NAME_PAIRS:
        if en in text.casefold():
            text = re.sub(re.escape(en), zh, text, flags=re.IGNORECASE)
    return text if _has_cjk(text) else None


def _primary_rate(rates: list[TuitionRate]) -> TuitionRate | None:
    official = [r for r in rates if not r.is_estimate]
    pool = official or rates
    if not pool:
        return None
    # Prefer international/non_local when present for outbound remittance planning.
    for status in ("international", "non_local", "out_of_state", None):
        hit = [r for r in pool if r.fee_status == status]
        if hit:
            return max(hit, key=lambda r: r.amount)
    return max(pool, key=lambda r: r.amount)


def payment_steps(payment: PaymentDetail | None) -> list[str]:
    if payment is None:
        return []
    steps = [
        f"缴费节奏：{BILLING_CYCLE_ZH.get(payment.billing_cycle, payment.billing_cycle)}。"
        + (f"分期约 {payment.installment_count} 次"
           f"（{INSTALLMENT_PERIOD_ZH.get(payment.installment_period, '按学校安排')}）。"
           if payment.installment_count else ""),
    ]
    if payment.due_rule:
        if _has_cjk(payment.due_rule):
            steps.append(f"截止日期规则：{payment.due_rule}")
        else:
            # English source rules stay out of the chat; keep a Chinese summary.
            steps.append("截止日期规则：以学校缴费通知书和缴费平台公布的截止日期为准。")
    if payment.payee:
        payee_zh = _zh_payee(payment.payee)
        steps.append(
            f"收款方：{payee_zh}。"
            if payee_zh
            else "收款方：以学校缴费通知书列明的收款账户为准。")
    if payment.portal_name or payment.portal_url:
        portal = PORTAL_NAME_ZH.get(payment.portal_name or "", "学校缴费门户")
        if payment.portal_url:
            steps.append(f"办理入口：{portal}（{payment.portal_url}）。")
        else:
            steps.append(f"办理入口：{portal}。")
    if payment.payment_methods:
        methods = [PAYMENT_METHOD_ZH.get(m, "") for m in payment.payment_methods]
        methods = [m for m in methods if m]
        if methods:
            steps.append("官方列出的支付方式：" + "、".join(methods) + "。")
    if payment.accepts_credit_card == 0:
        steps.append("学校说明：学生账户学费支付不接受信用卡/借记卡。")
    elif payment.accepts_credit_card == 1:
        steps.append("学校说明：可用信用卡支付学费（以缴费页面当日选项为准）。")
    if payment.supports_multi_currency == 1:
        channels = [CHANNEL_ZH.get(c, "") for c in payment.multi_currency_channels]
        channels = [c for c in channels if c]
        channels_txt = "、".join(channels) or "国际支付通道"
        steps.append(f"支持多币种/本地币支付通道：{channels_txt}。")
        if payment.multi_currency_note and _has_cjk(payment.multi_currency_note):
            # English source notes stay out of customer-facing Chinese steps.
            steps.append(payment.multi_currency_note)
    if payment.summary and _has_cjk(payment.summary):
        # English source quotes stay out of customer-facing Chinese steps;
        # they remain available in the payload for evidence.
        steps.append(payment.summary)
    return [s for s in steps if s]


def _monthly_spend_from_tuition(rate: TuitionRate | None, payment: PaymentDetail | None,
                                invoice_amount) -> Decimal | None:
    """Normalize tuition to an approximate monthly spend base for card scoring.

    Always aims for monthly * 12 ≈ one academic year's tuition charge — does not
    invent FX conversion; callers disclose the currency separately.
    """
    amount = None
    if invoice_amount is not None:
        amount = Decimal(str(invoice_amount))
    elif rate is not None:
        amount = Decimal(str(rate.amount))
    if amount is None or amount <= 0:
        return None
    basis = (rate.fee_basis if rate else None) or ""
    if basis == "monthly":
        return amount.quantize(Decimal("0.01"))
    if basis == "per_term":
        # Typical semester schools: 2 term bills ≈ one year.
        return (amount * Decimal("2") / Decimal("12")).quantize(Decimal("0.01"))
    if basis == "per_quarter":
        return (amount * Decimal("3") / Decimal("12")).quantize(Decimal("0.01"))
    # per_year / total / unknown: spread the published figure over 12 months.
    return (amount / Decimal("12")).quantize(Decimal("0.01"))


def _card_markets(school_country: str | None) -> list[str]:
    # Always include Hong Kong shelf (tuition reward + dual-currency data).
    markets = ["Hong Kong"]
    if school_country == "US":
        markets.append("US")
    elif school_country == "UK":
        markets.append("UK")
    return markets


def recommend_tuition_cards(*, tuition: TuitionContext, invoice_amount=None,
                            invoice_currency: str | None = None,
                            profile_facts: dict | None = None,
                            now: datetime | None = None) -> dict[str, Any]:
    """Rank cards for tuition settlement when multi-currency or card payment applies."""
    payment = tuition.payment
    result: dict[str, Any] = {
        "eligible_for_card_pitch": False,
        "reason": None,
        "scenarios": [],
        "recommendations": [],
        "limitations": [],
    }
    if payment is None:
        result["reason"] = "无缴费方式资料，暂不测算信用卡。"
        return result

    multi = payment.supports_multi_currency == 1
    accepts_card = payment.accepts_credit_card == 1
    if not multi and not accepts_card:
        result["reason"] = "当前资料未显示多币种通道或信用卡缴费，学费路径以学校列出的银行/电汇等方式为准。"
        return result

    result["eligible_for_card_pitch"] = True
    if accepts_card:
        result["reason"] = "学校接受信用卡缴学费；可以比较适用卡片的回馈、年费和支付成本。"
    else:
        result["reason"] = (
            "学校学生账户通常不收信用卡，但提供多币种/本地币通道（如 Flywire）。"
            "下列测算适用于：通道内可用卡支付的情形，或境外日常消费；"
            "Flywire 本地银行转账本身不经过信用卡。"
        )
        result["limitations"].append(
            "未计入 Flywire/Convera 平台加点或学校额外刷卡手续费（目录中无该费率则不估算）。"
        )

    rate = _primary_rate(tuition.rates)
    monthly = _monthly_spend_from_tuition(rate, payment, invoice_amount)
    scenarios = ["tuition"]
    # Foreign-currency tuition → also score overseas FX drag.
    # Use the currency of the amount we actually normalised. An invoice
    # currency without an invoice amount must not relabel the catalogue rate.
    currency = ((invoice_currency or "") if invoice_amount is not None
                else ((rate.currency or "") if rate else "")).upper()
    if currency and currency not in {"HKD", "CNY", "RMB"}:
        scenarios.append("overseas")
    result["scenarios"] = scenarios

    facts = dict(profile_facts or {})
    value_estimate_available = (monthly is not None and currency == "HKD"
                                and tuition.school_country not in {"US", "UK"})
    if value_estimate_available:
        facts["estimated_monthly_spend_hkd"] = str(monthly)
    else:
        facts.pop("estimated_monthly_spend_hkd", None)
    # Soft student hint improves student-card eligibility when profile is sparse.
    facts.setdefault("applicant_identity", "student")
    facts.setdefault("role", "student")

    repo = RdsCreditCardRepository()
    clock = now or datetime.now(timezone.utc)
    products = []
    try:
        for market in _card_markets(tuition.school_country):
            catalog = repo._load(market, clock)  # sync path; advice is sync-friendly
            products.extend(catalog.products)
    except CreditCardRepositoryError as exc:
        result["limitations"].append(f"信用卡目录暂不可用：{exc}")
        return result

    if not products:
        result["limitations"].append("当前市场无可用信用卡产品目录。")
        return result

    ranked = recommend(products, facts, scenarios, preference="no_preference", now=clock)
    picks = []
    ordered = []
    if ranked.primary:
        ordered.append(ranked.primary)
    ordered.extend(list(ranked.supplements or [])[:2])
    ordered.extend(list(ranked.alternatives or [])[:2])
    # Fall back to top ranked candidates when supplements/alternatives filters are empty.
    if len(ordered) < 2:
        for item in ranked.ranked_candidates or []:
            if item.card_id in {p.card_id for p in ordered}:
                continue
            ordered.append(item)
            if len(ordered) >= 3:
                break
    for item in ordered[:3]:
        picks.append({
            "card_id": item.card_id,
            "product_name": item.product_name,
            "issuing_bank": item.issuing_bank,
            "role": item.role,
            "matched_scenarios": list(item.matched_scenarios or []),
            "estimated_annual_reward": (float(item.estimated_annual_reward_hkd or 0)
                                        if value_estimate_available else None),
            "estimated_annual_fee": (float(item.estimated_annual_fee_hkd or 0)
                                     if value_estimate_available else None),
            "estimated_annual_fx_cost": (float(item.estimated_annual_fx_cost_hkd or 0)
                                        if value_estimate_available else None),
            "estimated_annual_net_value": (float(item.estimated_annual_net_value_hkd or 0)
                                          if value_estimate_available else None),
            "foreign_transaction_fee_rate": (
                float(item.foreign_transaction_fee_rate)
                if item.foreign_transaction_fee_rate is not None else None),
            "main_benefits": list(item.main_benefits or [])[:6],
            "official_url": item.official_url,
            "eligibility": item.eligibility.status if item.eligibility else None,
            "monthly_spend_basis": float(monthly) if monthly is not None else None,
            "currency_note": currency or None,
        })
    result["recommendations"] = picks
    result["value_estimate_available"] = value_estimate_available
    if monthly is not None:
        result["limitations"].append(
            f"目录学费折算月均约 {monthly} {currency or '（币种未确认）'}；"
            + ("仅在港币学费场景用于港币回馈估算。" if value_estimate_available else
               "没有可靠汇率，本次不展示港币净收益估算。"))
    if not picks:
        result["limitations"].append("有产品目录但无符合当前资格过滤的卡片。")
    return result


def card_steps(card_payload: dict[str, Any]) -> list[str]:
    steps: list[str] = []
    if not card_payload:
        return steps
    if card_payload.get("reason"):
        steps.append(card_payload["reason"])
    for lim in card_payload.get("limitations") or []:
        steps.append(lim)
    if not card_payload.get("eligible_for_card_pitch"):
        return steps
    recs = card_payload.get("recommendations") or []
    if not recs:
        return steps
    steps.append("与学费支付场景相关的卡片供比较；申请仍以银行审批为准：")
    for rec in recs:
        fx = rec.get("foreign_transaction_fee_rate")
        fx_txt = f"外币手续费约 {float(fx)*100:.2f}%" if fx is not None else "外币手续费未收录"
        matched = "、".join(
            SCENARIO_LABELS_ZH.get(s, s) for s in (rec.get("matched_scenarios") or [])
        ) or "一般消费"
        value = (f"年净收益约 {rec.get('estimated_annual_net_value')} 港元；"
                 if card_payload.get("value_estimate_available") else "")
        steps.append(
            f"· {rec.get('product_name')}（{rec.get('issuing_bank')}）："
            f"{value}适用场景 {matched}；{fx_txt}。"
            + (f"官网：{rec['official_url']}" if rec.get("official_url") else "")
        )
    return steps


def enrich_tuition_advice_steps(tuition: TuitionContext, *, invoice_amount=None,
                                invoice_currency: str | None = None,
                                profile_facts: dict | None = None) -> tuple[list[str], dict]:
    """Return extra answer steps + payload for payment/card enrichment."""
    steps: list[str] = []
    steps.extend(payment_steps(tuition.payment))
    cards = recommend_tuition_cards(
        tuition=tuition, invoice_amount=invoice_amount,
        invoice_currency=invoice_currency, profile_facts=profile_facts)
    steps.extend(card_steps(cards))
    payload = {
        "payment": tuition.payment.as_dict() if tuition.payment else None,
        "card_advice": cards,
    }
    return steps, payload
