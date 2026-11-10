"""Compose tuition payment + multi-currency + card-value advice for remittance.

Uses TUITION_PAYMENT_DETAIL and CREDIT_CARD catalog only — no invented FX/school fees.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from agent2.tuition_knowledge import (PaymentDetail, TuitionContext, TuitionRate,
                                      _format_money)

BILLING_CYCLE_ZH = {
    "per_year_advance": "通常按学年预付（一次缴清全年）",
    "per_term": "通常按学期账单缴费",
    "per_quarter": "通常按学季账单缴费",
    "monthly": "通常按月账单缴费",
    "installment": "通常可分期缴纳",
    "other": "以学校/项目缴费安排为准",
}

PAYMENT_METHOD_ZH = {
    "axess_echeck": "Axess 电子支票",
    "wire_flywire": "Flywire 国际汇款",
    "wire_convera": "Convera 国际汇款",
    "online_bank": "网上银行转账",
    "check_usd": "美元支票",
    "wire_bank": "银行电汇",
    "payroll_deduction": "工资代扣",
    "529": "529 教育储蓄计划",
    "flywire": "Flywire",
    "convera": "Convera",
    "credit_card": "信用卡",
    "debit_card": "借记卡",
    "ach": "ACH 转账",
    "bank_transfer": "银行转账",
}


def _method_label(code: str) -> str:
    key = (code or "").strip()
    return PAYMENT_METHOD_ZH.get(key, PAYMENT_METHOD_ZH.get(key.lower(), key.replace("_", " ")))


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
        steps.append(f"账单截止：{payment.due_rule}")
    if payment.payee:
        steps.append(f"收款方：{payment.payee}。")
    if payment.portal_name or payment.portal_url:
        portal = payment.portal_name or "学校缴费门户"
        if payment.portal_url:
            steps.append(f"缴费入口：{portal}（{payment.portal_url}）。")
        else:
            steps.append(f"缴费入口：{portal}。")
    if payment.payment_methods:
        labels = [_method_label(m) for m in payment.payment_methods]
        steps.append("可选支付方式：" + "、".join(labels) + "。")
    if payment.accepts_credit_card == 0:
        steps.append("学生账户不接受信用卡/借记卡直接缴费。")
    elif payment.accepts_credit_card == 1:
        steps.append("学生账户可用信用卡缴费（以缴费页当日选项为准）。")
    if payment.supports_multi_currency == 1:
        channels = "、".join(_method_label(c) for c in payment.multi_currency_channels) or "国际支付通道"
        steps.append(f"本地币/多币种可走：{channels}（可选币种与到账规则以通道当日为准）。")
    if payment.multi_currency_note and not _looks_redundant(payment.multi_currency_note, steps):
        steps.append(payment.multi_currency_note)
    if payment.summary and not _looks_redundant(payment.summary, steps):
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
        result["reason"] = "暂无学校缴费方式资料，先按学校账单上的汇款/转账路径办理。"
        return result

    multi = payment.supports_multi_currency == 1
    accepts_card = payment.accepts_credit_card == 1
    if not multi and not accepts_card:
        result["reason"] = "学校当前资料未显示信用卡或多币种刷卡通道，建议优先按账单上的电汇/银行转账办理。"
        return result

    result["eligible_for_card_pitch"] = True
    if accepts_card:
        result["reason"] = "学校接受信用卡缴学费时，可参考下列卡片。"
    else:
        # Card refusal is already stated in payment_steps; keep this short.
        result["reason"] = "若 Flywire 等通道当日支持刷卡，或用于境外日常消费，可参考下列卡片。"
        result["limitations"].append(
            "未计入支付平台加点或学校额外刷卡费。"
        )

    rate = _primary_rate(tuition.rates)
    monthly = _monthly_spend_from_tuition(rate, payment, invoice_amount)
    scenarios = ["tuition"]
    # Foreign-currency tuition → also score overseas FX drag.
    currency = (invoice_currency or (rate.currency if rate else "") or "").upper()
    if currency and currency not in {"HKD", "CNY", "RMB"}:
        scenarios.append("overseas")
    result["scenarios"] = scenarios

    facts = dict(profile_facts or {})
    if monthly is not None:
        facts["estimated_monthly_spend_hkd"] = str(monthly)
    # Soft student hint improves student-card eligibility when profile is sparse.
    facts.setdefault("applicant_identity", "student")
    facts.setdefault("role", "student")

    # Lazy imports: avoid agent2.orchestration package init ↔ fx ↔ this module cycle.
    from agent2.orchestration.domains.credit_card_engine import recommend
    from agent2.orchestration.domains.credit_card_repository import (
        CreditCardRepositoryError, RdsCreditCardRepository)

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
        })
    result["recommendations"] = picks
    if monthly is not None:
        money = _format_money(monthly, currency or None)
        result["limitations"].append(
            f"测算假设月均刷卡约 {money}。"
        )
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
    for rec in recs:
        fx = rec.get("foreign_transaction_fee_rate")
        fx_txt = f"外币手续费约 {float(fx)*100:.2f}%" if fx is not None else "外币手续费未收录"
        label = role_zh.get(rec.get("role") or "", rec.get("role") or "")
        net = rec.get("estimated_annual_net_value")
        reward = rec.get("estimated_annual_reward")
        fee = rec.get("estimated_annual_fee")
        fx_cost = rec.get("estimated_annual_fx_cost")
        line = (
            f"{label} {rec.get('product_name')}（{rec.get('issuing_bank')}）："
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
