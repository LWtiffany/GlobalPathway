"""Compose tuition payment + multi-currency + card-value advice for remittance.

Uses TUITION_PAYMENT_DETAIL and CREDIT_CARD catalog only — no invented FX/school fees.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from agent2.tuition_knowledge import (SCHOOL_NAME_ZH, PaymentDetail,
                                      TuitionContext, TuitionRate,
                                      _format_money)

BILLING_CYCLE_ZH = {
    "per_year_advance": "通常按学年预付（一次缴清全年）",
    "per_term": "通常按学期账单缴费",
    "per_quarter": "通常按学季账单缴费",
    "monthly": "通常按月账单缴费",
    "installment": "通常可分期缴纳",
    "other": "以学校/项目缴费安排为准",
}

# Catalog method ids → Chinese labels. Unmapped ids are dropped from the
# customer-facing line (they stay in the payload) — snake_case never leaks.
PAYMENT_METHOD_ZH = {
    "529": "529 教育储蓄计划",
    "ach": "ACH 转账",
    "ach_epayment": "ACH 电子支付",
    "atm": "ATM",
    "atm_bill": "ATM 账单缴费",
    "axess_echeck": "Axess 电子支票",
    "bank_counter": "银行柜台",
    "bank_counter_atm": "银行柜台/ATM",
    "bank_draft": "银行汇票",
    "bank_remittance_wire": "银行电汇",
    "bank_transfer": "银行转账",
    "cash_in_office": "校内现金缴费",
    "check_usd": "美元支票",
    "check_usd_us_bank": "美元支票（美国银行账户）",
    "cheque_deposit_machine": "支票存款机",
    "college_arrangements": "按学院安排缴费",
    "credit_card": "信用卡",
    "cuhk_fee_payment_channels": "中大官方缴费渠道",
    "cusis_online": "CUSIS 在线缴费",
    "debit_card": "借记卡",
    "department_direct": "院系直接收款",
    "flywire": "Flywire",
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
    "wire_convera": "Convera 国际汇款",
    "wire_flywire": "Flywire 国际汇款",
    "wire_transfermate": "TransferMate 电汇",
    "yale_payment_plan": "耶鲁分期缴费计划",
    "yalepay_ach": "YalePay ACH 缴费",
}

# Catalog portal names → Chinese labels (system names like CUSIS stay as-is).
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


def _method_label(code: str) -> str:
    """Chinese label for a catalog method/channel id; empty when unmapped.

    Unmapped ids return "" so the render layer can drop them from the
    customer-facing line instead of leaking a snake_case identifier.
    """
    key = (code or "").strip()
    return PAYMENT_METHOD_ZH.get(key, PAYMENT_METHOD_ZH.get(key.lower(), ""))


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


def _looks_redundant(note: str, already: list[str]) -> bool:
    """Skip catalog notes that mostly restate billing / channel / card facts already listed."""
    text = (note or "").strip().lower()
    if not text:
        return True
    blob = " ".join(already).lower()
    tokens = ("flywire", "convera", "credit card", "debit", "信用卡", "借记卡",
              "monthly", "按月", "多币种", "本地币", "axess", "529")
    hits = sum(1 for t in tokens if t in text and t in blob)
    # English summary that repeats monthly billing / preferred channels.
    if hits >= 2:
        return True
    if text.startswith("官方列出") and ("flywire" in blob or "多币种" in blob):
        return True
    return False


def payment_steps(payment: PaymentDetail | None) -> list[str]:
    if payment is None:
        return []
    steps: list[str] = []
    cycle = BILLING_CYCLE_ZH.get(payment.billing_cycle, payment.billing_cycle)
    if payment.installment_count:
        period = {"month": "按月", "term": "按学期", "year": "按年"}.get(
            (payment.installment_period or "").lower(), payment.installment_period or "")
        if period and period in cycle:
            steps.append(f"{cycle}，约分 {payment.installment_count} 次。")
        else:
            suffix = f"（{period}）" if period else ""
            steps.append(f"{cycle}，约分 {payment.installment_count} 次缴纳{suffix}。")
    else:
        steps.append(f"{cycle}。")
    if payment.due_rule:
        if _has_cjk(payment.due_rule):
            steps.append(f"账单截止：{payment.due_rule}")
        else:
            # English source rules stay out of the chat; keep a Chinese summary.
            steps.append("账单截止：以学校缴费通知书和缴费平台公布的截止日期为准。")
    if payment.payee:
        payee_zh = _zh_payee(payment.payee)
        steps.append(
            f"收款方：{payee_zh}。"
            if payee_zh
            else "收款方：以学校缴费通知书列明的收款账户为准。")
    if payment.portal_name or payment.portal_url:
        portal = PORTAL_NAME_ZH.get((payment.portal_name or "").strip(), "") or "学校缴费门户"
        if payment.portal_url:
            steps.append(f"缴费入口：{portal}（{payment.portal_url}）。")
        else:
            steps.append(f"缴费入口：{portal}。")
    if payment.payment_methods:
        labels = [lbl for lbl in (_method_label(m) for m in payment.payment_methods) if lbl]
        if labels:
            steps.append("可选支付方式：" + "、".join(labels) + "。")
    if payment.accepts_credit_card == 0:
        steps.append("学生账户不接受信用卡/借记卡直接缴费。")
    elif payment.accepts_credit_card == 1:
        steps.append("学生账户可用信用卡缴费（以缴费页当日选项为准）。")
    if payment.supports_multi_currency == 1:
        channels = "、".join(lbl for lbl in
                            (CHANNEL_ZH.get((c or "").strip(), _method_label(c))
                             for c in payment.multi_currency_channels) if lbl) \
            or "国际支付通道"
        steps.append(f"本地币/多币种可走：{channels}（可选币种与到账规则以通道当日为准）。")
    if payment.multi_currency_note and _has_cjk(payment.multi_currency_note) \
            and not _looks_redundant(payment.multi_currency_note, steps):
        # English source notes stay out of the customer-facing Chinese steps.
        steps.append(payment.multi_currency_note)
    if payment.summary and _has_cjk(payment.summary) \
            and not _looks_redundant(payment.summary, steps):
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


def _normalize_school_country(school_country: str | None) -> str | None:
    raw = (school_country or "").strip()
    if not raw:
        return None
    key = raw.upper().replace(".", "")
    if key in {"US", "USA", "UNITED STATES", "UNITED STATES OF AMERICA", "美国"}:
        return "US"
    if key in {"UK", "GB", "GBR", "UNITED KINGDOM", "GREAT BRITAIN", "英国"}:
        return "UK"
    if key in {"HK", "CN-HK", "HONG KONG", "香港", "中国香港"}:
        return "Hong Kong"
    return raw


def _local_card_market(school_country: str | None) -> str | None:
    """Study destination shelf for tuition cards — prefer local issuance to avoid FX."""
    country = _normalize_school_country(school_country)
    if country in {"US", "UK"}:
        return country
    if country == "Hong Kong":
        return "Hong Kong"
    return None


def _pick_ordered(ranked, *, limit: int = 3) -> list:
    """Primary → supplements → alternatives → ranked_candidates, de-duplicated."""
    ordered = []
    if ranked.primary:
        ordered.append(ranked.primary)
    ordered.extend(list(ranked.supplements or [])[:2])
    ordered.extend(list(ranked.alternatives or [])[:2])
    if len(ordered) < limit:
        for item in ranked.ranked_candidates or []:
            if item.card_id in {p.card_id for p in ordered}:
                continue
            ordered.append(item)
            if len(ordered) >= limit:
                break
    return ordered[:limit]


def _serialize_card_pick(item, *, role: str | None = None, monthly=None,
                        currency: str | None = None) -> dict[str, Any]:
    return {
        "card_id": item.card_id,
        "product_name": item.product_name,
        "issuing_bank": item.issuing_bank,
        "role": role or item.role,
        "market": getattr(item, "market", None),
        "matched_scenarios": list(item.matched_scenarios or []),
        "estimated_annual_reward": float(item.estimated_annual_reward_hkd or 0),
        "estimated_annual_fee": float(item.estimated_annual_fee_hkd or 0),
        "estimated_annual_fx_cost": float(item.estimated_annual_fx_cost_hkd or 0),
        "estimated_annual_net_value": float(item.estimated_annual_net_value_hkd or 0),
        "foreign_transaction_fee_rate": (
            float(item.foreign_transaction_fee_rate)
            if item.foreign_transaction_fee_rate is not None else None),
        "main_benefits": list(item.main_benefits or [])[:6],
        "official_url": item.official_url,
        "eligibility": item.eligibility.status if item.eligibility else None,
        "monthly_spend_basis": float(monthly) if monthly is not None else None,
        "currency_note": currency or None,
    }


def recommend_tuition_cards(*, tuition: TuitionContext, invoice_amount=None,
                            invoice_currency: str | None = None,
                            profile_facts: dict | None = None,
                            now: datetime | None = None) -> dict[str, Any]:
    """Rank cards for tuition settlement when multi-currency or card payment applies.

    US/UK tuition: prefer the local HSBC shelf first (avoids HK foreign-transaction
    fees on USD/GBP bills). Hong Kong cards are backup only, never mixed into the
    same net-value ranking as local cards.
    """
    payment = tuition.payment
    result: dict[str, Any] = {
        "eligible_for_card_pitch": False,
        "reason": None,
        "scenarios": [],
        "recommendations": [],
        "limitations": [],
        "preferred_market": None,
    }
    if payment is None:
        result["reason"] = "暂无学校缴费方式资料，先按学校账单上的汇款/转账路径办理。"
        return result

    multi = payment.supports_multi_currency == 1
    accepts_card = payment.accepts_credit_card == 1
    if not multi and not accepts_card:
        result["reason"] = "学校当前资料未显示信用卡或多币种刷卡通道，建议优先按账单上的电汇/银行转账办理。"
        return result

    local_market = _local_card_market(tuition.school_country)
    result["preferred_market"] = local_market or "Hong Kong"
    result["eligible_for_card_pitch"] = True
    place_zh = {"US": "美国", "UK": "英国", "Hong Kong": "香港"}.get(
        local_market or "", "当地")
    if local_market in {"US", "UK"}:
        result["reason"] = (
            f"缴{place_zh}学费优先参考{place_zh}发行的卡，通常可减少外币手续费。"
        )
    elif accepts_card:
        result["reason"] = "学校接受信用卡缴学费时，可参考下列卡片。"
    else:
        result["reason"] = "若 Flywire 等通道当日支持刷卡，或用于境外日常消费，可参考下列卡片。"
    result["limitations"].append("未计入支付平台加点或学校额外刷卡费。")

    rate = _primary_rate(tuition.rates)
    monthly = _monthly_spend_from_tuition(rate, payment, invoice_amount)
    currency = (invoice_currency or (rate.currency if rate else "") or "").upper()
    # Local shelf pays tuition in local currency → no FX scenario.
    # HK backup on USD/GBP bills → include overseas so FX fees are scored.
    local_scenarios = ["tuition"]
    hk_scenarios = ["tuition"]
    foreign_bill = bool(currency and currency not in {"HKD", "CNY", "RMB"})
    if foreign_bill and local_market not in {"US", "UK"}:
        local_scenarios.append("overseas")
    if foreign_bill:
        hk_scenarios.append("overseas")
    result["scenarios"] = local_scenarios

    facts = dict(profile_facts or {})
    if monthly is not None:
        facts["estimated_monthly_spend_hkd"] = str(monthly)
    # Soft student hint helps student-card eligibility when profile is sparse.
    facts.setdefault("applicant_identity", "student")
    facts.setdefault("role", "student")
    if local_market == "US":
        facts.setdefault("application_region", "US")
    elif local_market == "UK":
        facts.setdefault("application_region", "UK")

    # Lazy imports: avoid agent2.orchestration package init ↔ fx ↔ this module cycle.
    from agent2.orchestration.domains.credit_card_engine import recommend
    from agent2.orchestration.domains.credit_card_repository import (
        CreditCardRepositoryError, RdsCreditCardRepository)

    repo = RdsCreditCardRepository()
    clock = now or datetime.now(timezone.utc)

    def _load(market: str):
        return repo._load(market, clock).products

    try:
        local_products = _load(local_market) if local_market else []
        hk_products = [] if local_market == "Hong Kong" else _load("Hong Kong")
        if not local_market:
            local_products = _load("Hong Kong")
            hk_products = []
    except CreditCardRepositoryError as exc:
        result["limitations"].append(f"信用卡目录暂不可用：{exc}")
        return result

    if not local_products and not hk_products:
        result["limitations"].append("当前市场无可用信用卡产品目录。")
        return result

    picks: list[dict[str, Any]] = []
    # 1) Local shelf first — never cross-rank with HK HKD net value.
    if local_products:
        local_ranked = recommend(
            local_products, facts, local_scenarios, preference="no_preference", now=clock)
        for idx, item in enumerate(_pick_ordered(local_ranked, limit=3)):
            role = "primary" if idx == 0 else "alternative"
            picks.append(_serialize_card_pick(
                item, role=role, monthly=monthly, currency=currency))
            picks[-1]["market"] = local_market

    # 2) HK only as backup when local shelf is thin (FX fee on USD/GBP tuition).
    need_backup = len(picks) < 2 and hk_products
    if need_backup:
        hk_ranked = recommend(
            hk_products, facts, hk_scenarios, preference="no_preference", now=clock)
        for item in _pick_ordered(hk_ranked, limit=3 - len(picks)):
            picks.append(_serialize_card_pick(
                item, role="alternative", monthly=monthly, currency=currency))
            picks[-1]["market"] = "Hong Kong"
        if local_market in {"US", "UK"} and any(p.get("market") == "Hong Kong" for p in picks):
            result["limitations"].append(
                f"香港卡仅作备用：刷{place_zh}学费通常产生外币手续费，优先考虑{place_zh}本地卡。"
            )

    result["recommendations"] = picks
    if monthly is not None:
        money = _format_money(monthly, currency or None)
        result["limitations"].append(f"测算假设月均刷卡约 {money}。")
    if not picks:
        result["limitations"].append("有产品目录但无符合当前资格过滤的卡片。")
    return result


def card_steps(card_payload: dict[str, Any]) -> list[str]:
    """Card pick lines only — no policy restatement, no bullet markers."""
    steps: list[str] = []
    if not card_payload or not card_payload.get("eligible_for_card_pitch"):
        return steps
    recs = card_payload.get("recommendations") or []
    if not recs:
        return steps
    role_zh = {"primary": "首选", "alternative": "备选", "supplement": "补充"}
    market_zh = {"US": "美国", "UK": "英国", "Hong Kong": "香港", "CN-HK": "香港"}
    for rec in recs:
        fx = rec.get("foreign_transaction_fee_rate")
        fx_txt = f"外币手续费约 {float(fx)*100:.2f}%" if fx is not None else "外币手续费未收录"
        label = role_zh.get(rec.get("role") or "", rec.get("role") or "")
        market = market_zh.get(rec.get("market") or "", rec.get("market") or "")
        bank = rec.get("issuing_bank") or ""
        issuer = f"{bank}·{market}" if market else bank
        net = rec.get("estimated_annual_net_value")
        reward = rec.get("estimated_annual_reward")
        fee = rec.get("estimated_annual_fee")
        fx_cost = rec.get("estimated_annual_fx_cost")
        line = (
            f"{label} {rec.get('product_name')}（{issuer}）："
            f"年净收益约 {net}（回馈 {reward} − 年费 {fee} − 外币成本 {fx_cost}；{fx_txt}）"
        )
        if rec.get("official_url"):
            line += f"。详情：{rec['official_url']}"
        steps.append(line)
    return steps


def enrich_tuition_advice_steps(tuition: TuitionContext, *, invoice_amount=None,
                                invoice_currency: str | None = None,
                                profile_facts: dict | None = None) -> tuple[list[str], dict]:
    """Return school-payment steps + payload; card picks live under card_advice.steps."""
    pay = payment_steps(tuition.payment)
    cards = recommend_tuition_cards(
        tuition=tuition, invoice_amount=invoice_amount,
        invoice_currency=invoice_currency, profile_facts=profile_facts)
    cards["steps"] = card_steps(cards)
    payload = {
        "payment": tuition.payment.as_dict() if tuition.payment else None,
        "card_advice": cards,
    }
    return pay, payload
