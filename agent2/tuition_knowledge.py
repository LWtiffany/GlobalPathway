"""Read SCHOOL / PROGRAM / TUITION from Agent1 RDS for remittance advice.

All amounts and labels come from the working database — nothing is hardcoded
per school. Callers pass profile facts (school name, course, destination).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Callable

from agent1.storage import read_session

# Profile / UI often stores SCHOOL.school_id / PROGRAM.program_id as the fact value.
_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)


def _looks_like_id(value: str | None) -> bool:
    return bool(value and _UUID_RE.match(value.strip()))

# fee_status values allowed by TUITION check constraint → Chinese label for display.
# Keys are schema enums, not school-specific rates.
FEE_STATUS_ZH = {
    "home": "本地生",
    "international": "国际生",
    "local": "本地生",
    "non_local": "非本地生",
    "in_state": "州内",
    "out_of_state": "州外",
    "all": "统一费率",
}

FEE_BASIS_ZH = {
    "per_year": "全年",
    "per_term": "每学期",
    "per_quarter": "每学季",
    "monthly": "每月",
    "total": "整个项目",
    "per_credit": "每学分",
    "other": "",
}

STUDY_MODE_ZH = {
    "full_time": "全日制",
    "part_time": "兼读制",
    "full-time": "全日制",
    "part-time": "兼读制",
}


def _format_money(amount, currency: str | None) -> str:
    try:
        value = Decimal(str(amount))
    except Exception:
        return f"{amount} {currency or ''}".strip()
    if value == value.to_integral_value():
        rendered = f"{int(value):,}"
    else:
        rendered = f"{value:,.2f}"
    cur = (currency or "").upper()
    if cur == "USD":
        return f"{rendered} 美元"
    if cur == "GBP":
        return f"{rendered} 英镑"
    if cur == "HKD":
        return f"{rendered} 港元"
    if cur in {"CNY", "RMB"}:
        return f"{rendered} 人民币"
    if cur == "EUR":
        return f"{rendered} 欧元"
    return f"{rendered} {cur}".strip()


@dataclass(frozen=True)
class TuitionRate:
    amount: Decimal
    currency: str
    academic_year: str | None
    fee_status: str | None
    fee_basis: str | None
    study_mode: str | None
    rate_label: str | None
    remark: str | None
    source_url: str | None
    is_estimate: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "amount": float(self.amount) if self.amount is not None else None,
            "currency": self.currency,
            "academic_year": self.academic_year,
            "fee_status": self.fee_status,
            "fee_status_label": FEE_STATUS_ZH.get(self.fee_status or "", self.fee_status),
            "fee_basis": self.fee_basis,
            "study_mode": self.study_mode,
            "rate_label": self.rate_label,
            "remark": self.remark,
            "source_url": self.source_url,
            "is_estimate": bool(self.is_estimate),
        }


@dataclass(frozen=True)
class PaymentDetail:
    detail_id: str
    scope: str
    level: str | None
    program_id: str | None
    academic_year: str | None
    billing_cycle: str
    installment_count: int | None
    installment_period: str | None
    payment_methods: list[str]
    accepts_credit_card: int | None
    supports_multi_currency: int | None
    multi_currency_channels: list[str]
    multi_currency_note: str | None
    portal_name: str | None
    portal_url: str | None
    payee: str | None
    due_rule: str | None
    summary: str
    quote: str
    source_url: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "detail_id": self.detail_id,
            "scope": self.scope,
            "level": self.level,
            "program_id": self.program_id,
            "academic_year": self.academic_year,
            "billing_cycle": self.billing_cycle,
            "installment_count": self.installment_count,
            "installment_period": self.installment_period,
            "payment_methods": list(self.payment_methods),
            "accepts_credit_card": self.accepts_credit_card,
            "supports_multi_currency": self.supports_multi_currency,
            "multi_currency_channels": list(self.multi_currency_channels),
            "multi_currency_note": self.multi_currency_note,
            "portal_name": self.portal_name,
            "portal_url": self.portal_url,
            "payee": self.payee,
            "due_rule": self.due_rule,
            "summary": self.summary,
            "quote": self.quote,
            "source_url": self.source_url,
        }


@dataclass
class TuitionContext:
    school_id: str | None = None
    school_name: str | None = None
    school_country: str | None = None
    program_id: str | None = None
    program_name: str | None = None
    program_level: str | None = None
    program_scope: str | None = None
    rates: list[TuitionRate] = field(default_factory=list)
    payment: PaymentDetail | None = None
    match: str = "none"  # exact_program | school_wide | school_only | ambiguous_school | ambiguous_program | not_found
    candidates: list[dict[str, Any]] = field(default_factory=list)
    note: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "match": self.match,
            "school_id": self.school_id,
            "school_name": self.school_name,
            "school_country": self.school_country,
            "program_id": self.program_id,
            "program_name": self.program_name,
            "program_level": self.program_level,
            "program_scope": self.program_scope,
            "rates": [r.as_dict() for r in self.rates],
            "payment": self.payment.as_dict() if self.payment else None,
            "candidates": self.candidates,
            "note": self.note,
        }


def _norm(text: str | None) -> str:
    return " ".join((text or "").casefold().split())


def _rate_from_row(row) -> TuitionRate:
    return TuitionRate(
        amount=Decimal(str(row["amount"])),
        currency=row["currency"],
        academic_year=row["academic_year"],
        fee_status=row["fee_status"],
        fee_basis=row["fee_basis"],
        study_mode=row["study_mode"],
        rate_label=row["rate_label"],
        remark=row["remark"],
        source_url=row["source_url"],
        is_estimate=int(row["is_estimate"] or 0),
    )


def _load_rates(con, program_id: str) -> list[TuitionRate]:
    rows = con.execute(
        """SELECT amount, currency, academic_year, fee_status, fee_basis, study_mode,
                  rate_label, remark, source_url, is_estimate
           FROM tuition
           WHERE program_id=%s
           ORDER BY is_estimate ASC, academic_year DESC NULLS LAST, fee_status NULLS FIRST""",
        (program_id,),
    ).fetchall()
    rates = [_rate_from_row(r) for r in rows]
    official = [r for r in rates if not r.is_estimate]
    return official or rates


def _payment_from_row(row) -> PaymentDetail:
    methods = row["payment_methods"] or []
    channels = row.get("multi_currency_channels") or []
    if isinstance(methods, str):
        import json
        methods = json.loads(methods)
    if isinstance(channels, str):
        import json
        channels = json.loads(channels)
    return PaymentDetail(
        detail_id=row["detail_id"],
        scope=row["scope"],
        level=row["level"],
        program_id=row["program_id"],
        academic_year=row["academic_year"],
        billing_cycle=row["billing_cycle"],
        installment_count=row["installment_count"],
        installment_period=row["installment_period"],
        payment_methods=list(methods),
        accepts_credit_card=None if row["accepts_credit_card"] is None
        else int(row["accepts_credit_card"]),
        supports_multi_currency=None if row.get("supports_multi_currency") is None
        else int(row["supports_multi_currency"]),
        multi_currency_channels=list(channels or []),
        multi_currency_note=row.get("multi_currency_note"),
        portal_name=row["portal_name"],
        portal_url=row["portal_url"],
        payee=row["payee"],
        due_rule=row["due_rule"],
        summary=row["summary"],
        quote=row["quote"],
        source_url=row["source_url"],
    )


def _load_payment_detail(con, *, school_id: str, program_id: str | None,
                         level: str | None) -> PaymentDetail | None:
    """Prefer programme override, then level, then school_wide."""
    if program_id:
        row = con.execute(
            """SELECT * FROM tuition_payment_detail
               WHERE school_id=%s AND scope='programme' AND program_id=%s
               ORDER BY recorded_at DESC NULLS LAST LIMIT 1""",
            (school_id, program_id),
        ).fetchone()
        if row:
            return _payment_from_row(row)
    if level:
        row = con.execute(
            """SELECT * FROM tuition_payment_detail
               WHERE school_id=%s AND scope='level' AND level=%s
               ORDER BY recorded_at DESC NULLS LAST LIMIT 1""",
            (school_id, level),
        ).fetchone()
        if row:
            return _payment_from_row(row)
    row = con.execute(
        """SELECT * FROM tuition_payment_detail
           WHERE school_id=%s AND scope='school_wide'
           ORDER BY recorded_at DESC NULLS LAST LIMIT 1""",
        (school_id,),
    ).fetchone()
    return _payment_from_row(row) if row else None


def _find_schools(con, school_query: str, destination_country: str | None):
    q = school_query.strip()
    if not q:
        return []
    # Demo/profile stores school as SCHOOL.school_id UUID; resolve by PK first.
    if _looks_like_id(q):
        row = con.execute(
            """SELECT school_id, name, country, city
               FROM school WHERE school_id=%s""",
            (q,),
        ).fetchone()
        return [row] if row else []
    rows = con.execute(
        """SELECT school_id, name, country, city
           FROM school
           WHERE name ILIKE %s
           ORDER BY
             CASE WHEN lower(name)=lower(%s) THEN 0
                  WHEN lower(name) LIKE lower(%s) THEN 1
                  ELSE 2 END,
             name
           LIMIT 12""",
        (f"%{q}%", q, f"{q}%"),
    ).fetchall()
    if destination_country:
        filtered = [r for r in rows if r["country"] == destination_country]
        if filtered:
            rows = filtered
    return rows


def _score_program(name: str, level: str | None, course_q: str) -> int:
    n, c = _norm(name), _norm(course_q)
    if not c:
        return 0
    if n == c:
        return 100
    if c in n or n in c:
        return 80
    # token overlap
    nt, ct = set(n.split()), set(c.split())
    if not ct:
        return 0
    overlap = len(nt & ct) / len(ct)
    score = int(60 * overlap)
    if level and _norm(level) in c:
        score += 5
    return score


def _pick_school_wide(con, school_id: str):
    rows = con.execute(
        """SELECT program_id, name, level, scope FROM program
           WHERE school_id=%s AND scope='school_wide'
           ORDER BY CASE level
             WHEN 'undergraduate' THEN 1
             WHEN 'bachelor' THEN 1
             WHEN 'graduate' THEN 2
             WHEN 'master' THEN 3
             WHEN 'phd' THEN 4
             ELSE 9 END, name""",
        (school_id,),
    ).fetchall()
    for row in rows:
        rates = _load_rates(con, row["program_id"])
        if rates:
            return row, rates
    return None, []


def _attach_payment(con, ctx: TuitionContext) -> TuitionContext:
    if not ctx.school_id:
        return ctx
    level = None
    if ctx.program_level and "," not in ctx.program_level:
        level = ctx.program_level
    ctx.payment = _load_payment_detail(
        con, school_id=ctx.school_id, program_id=ctx.program_id, level=level)
    return ctx


def resolve_tuition(*, school: str | None, course: str | None = None,
                    destination_country: str | None = None) -> TuitionContext:
    """Resolve published tuition from RDS using free-text school / course facts."""
    ctx = TuitionContext()
    if not school or not str(school).strip():
        ctx.match = "none"
        ctx.note = "未提供学校，无法匹配学费目录。"
        return ctx

    with read_session() as con:
        schools = _find_schools(con, str(school), destination_country)
        if not schools:
            ctx.match = "not_found"
            ctx.note = f"学费目录中未找到与「{school}」匹配的学校。"
            return ctx
        if len(schools) > 1 and _norm(schools[0]["name"]) != _norm(school):
            # multiple fuzzy hits and none exact
            exact = [s for s in schools if _norm(s["name"]) == _norm(school)]
            if len(exact) == 1:
                schools = exact
            elif not exact:
                ctx.match = "ambiguous_school"
                ctx.candidates = [
                    {"school_id": s["school_id"], "name": s["name"], "country": s["country"]}
                    for s in schools[:8]
                ]
                ctx.note = "匹配到多所学校，请确认学校全称。"
                return ctx

        school_row = schools[0]
        ctx.school_id = school_row["school_id"]
        ctx.school_name = school_row["name"]
        ctx.school_country = school_row["country"]

        if course and str(course).strip():
            course_q = str(course).strip()
            # Profile may store PROGRAM.program_id; hit that row directly.
            if _looks_like_id(course_q):
                by_id = con.execute(
                    """SELECT program_id, name, level, scope FROM program
                       WHERE school_id=%s AND program_id=%s""",
                    (ctx.school_id, course_q),
                ).fetchone()
                if by_id:
                    rates = _load_rates(con, by_id["program_id"])
                    ctx.program_id = by_id["program_id"]
                    ctx.program_name = by_id["name"]
                    ctx.program_level = by_id["level"]
                    ctx.program_scope = by_id["scope"]
                    ctx.rates = rates
                    ctx.match = "exact_program" if rates else "school_only"
                    if not rates:
                        ctx.note = (
                            f"已匹配「{ctx.school_name} / {ctx.program_name}」，"
                            "但目录中尚无公布学费金额。"
                        )
                    return _attach_payment(con, ctx)
            programs = con.execute(
                """SELECT program_id, name, level, scope FROM program
                   WHERE school_id=%s
                   ORDER BY name""",
                (ctx.school_id,),
            ).fetchall()
            scored = []
            for p in programs:
                score = _score_program(p["name"], p["level"], course_q)
                if score >= 40:
                    scored.append((score, p))
            scored.sort(key=lambda x: (-x[0], x[1]["name"]))
            if not scored:
                ctx.match = "school_only"
                ctx.note = f"已匹配学校「{ctx.school_name}」，但未找到与「{course}」对应的项目学费。"
                sw, rates = _pick_school_wide(con, ctx.school_id)
                if sw and rates:
                    ctx.program_id = sw["program_id"]
                    ctx.program_name = sw["name"]
                    ctx.program_level = sw["level"]
                    ctx.program_scope = sw["scope"]
                    ctx.rates = rates
                    ctx.match = "school_wide"
                    ctx.note = (f"未精确匹配课程「{course}」，先给出该校 school-wide 公布学费；"
                                "请以录取项目缴费通知为准，或补充更准确的课程名称。")
                return _attach_payment(con, ctx)
            top = scored[0][0]
            top_rows = [p for s, p in scored if s == top]
            if len(top_rows) > 1:
                same_name = len({_norm(p["name"]) for p in top_rows}) == 1
                level_hint = None
                cq = _norm(str(course))
                for token, level in (
                    ("bachelor", "bachelor"), ("undergraduate", "bachelor"),
                    ("master", "master"), ("mphil", "master"), ("msc", "master"),
                    ("mba", "master"), ("phd", "phd"), ("doctoral", "phd"),
                    ("graduate", "graduate"), ("professional", "professional"),
                ):
                    if token in cq:
                        level_hint = level
                        break
                if level_hint:
                    hinted = [p for p in top_rows if (p["level"] or "") == level_hint]
                    if len(hinted) == 1:
                        top_rows = hinted
                if len(top_rows) > 1 and same_name:
                    # Same programme title at multiple levels: return all official rates,
                    # labelled by level — still no hardcoded school/amount.
                    all_rates: list[TuitionRate] = []
                    for prog in top_rows:
                        for rate in _load_rates(con, prog["program_id"]):
                            label = rate.rate_label or prog["level"]
                            all_rates.append(TuitionRate(
                                amount=rate.amount, currency=rate.currency,
                                academic_year=rate.academic_year,
                                fee_status=rate.fee_status, fee_basis=rate.fee_basis,
                                study_mode=rate.study_mode, rate_label=label,
                                remark=rate.remark, source_url=rate.source_url,
                                is_estimate=rate.is_estimate,
                            ))
                    ctx.program_id = top_rows[0]["program_id"]
                    ctx.program_name = top_rows[0]["name"]
                    ctx.program_level = ",".join(
                        sorted({p["level"] for p in top_rows if p["level"]}))
                    ctx.rates = all_rates
                    ctx.match = "exact_program" if all_rates else "school_only"
                    ctx.candidates = [
                        {"program_id": p["program_id"], "name": p["name"], "level": p["level"]}
                        for p in top_rows
                    ]
                    ctx.note = (
                        f"「{ctx.program_name}」在目录中有多个层次（{ctx.program_level}）；"
                        "下列为各层次公布学费，请按录取层次与缴费通知书选择。"
                    )
                    return _attach_payment(con, ctx)
                if len(top_rows) > 1:
                    ctx.match = "ambiguous_program"
                    ctx.candidates = [
                        {"program_id": p["program_id"], "name": p["name"], "level": p["level"]}
                        for p in top_rows[:10]
                    ]
                    ctx.note = "匹配到多个相近项目，请确认课程名称。"
                    return _attach_payment(con, ctx)
            prog = top_rows[0]
            rates = _load_rates(con, prog["program_id"])
            ctx.program_id = prog["program_id"]
            ctx.program_name = prog["name"]
            ctx.program_level = prog["level"]
            ctx.program_scope = prog["scope"]
            ctx.rates = rates
            ctx.match = "exact_program" if rates else "school_only"
            if not rates:
                ctx.note = f"已匹配「{ctx.school_name} / {ctx.program_name}」，但目录中尚无学费行。"
            return _attach_payment(con, ctx)

        # No course: prefer school-wide published rate; else ask for course.
        sw, rates = _pick_school_wide(con, ctx.school_id)
        if sw and rates:
            ctx.program_id = sw["program_id"]
            ctx.program_name = sw["name"]
            ctx.program_level = sw["level"]
            ctx.program_scope = sw["scope"]
            ctx.rates = rates
            ctx.match = "school_wide"
            ctx.note = ("当前按该校 school-wide 公布学费整理；"
                        "研究生/专业项目费率常因项目而异，补充课程名称可给出更精确数字。")
            return _attach_payment(con, ctx)

        # List a few programmes that already have official tuition so the user can pick.
        with_fees = con.execute(
            """SELECT p.program_id, p.name, p.level,
                      COUNT(*) FILTER (WHERE t.is_estimate=0) AS official_n
               FROM program p
               JOIN tuition t ON t.program_id=p.program_id
               WHERE p.school_id=%s
               GROUP BY p.program_id, p.name, p.level
               HAVING COUNT(*) FILTER (WHERE t.is_estimate=0) > 0
               ORDER BY p.name
               LIMIT 12""",
            (ctx.school_id,),
        ).fetchall()
        if with_fees:
            ctx.match = "ambiguous_program"
            ctx.candidates = [
                {"program_id": r["program_id"], "name": r["name"], "level": r["level"],
                 "official_rates": int(r["official_n"])}
                for r in with_fees
            ]
            ctx.note = f"「{ctx.school_name}」学费因项目而异，请补充课程/学位名称。"
            return _attach_payment(con, ctx)

        ctx.match = "school_only"
        ctx.note = f"已匹配学校「{ctx.school_name}」，但目录中尚无可用学费行。"
        return _attach_payment(con, ctx)


def format_rate_line(rate: TuitionRate, *, school: str | None = None,
                     program: str | None = None) -> str:
    """One human-readable tuition line (no invented numbers, no raw DB enums)."""
    bits = []
    if school:
        bits.append(school)
    if program:
        bits.append(program)
    if rate.academic_year:
        bits.append(f"{rate.academic_year} 学年")
    status = FEE_STATUS_ZH.get(rate.fee_status or "", rate.fee_status)
    if status:
        bits.append(status)
    mode = STUDY_MODE_ZH.get((rate.study_mode or "").lower(), rate.study_mode)
    if mode:
        bits.append(mode)
    if rate.rate_label and str(rate.rate_label) not in bits:
        bits.append(str(rate.rate_label))
    head = " · ".join(bits) if bits else "学费参考"
    basis = FEE_BASIS_ZH.get(rate.fee_basis or "", rate.fee_basis or "")
    money = _format_money(rate.amount, rate.currency)
    if basis:
        money = f"{basis} {money}"
    est = "（估算，请以缴费通知为准）" if rate.is_estimate else ""
    return f"{head}：{money}{est}"


def compare_to_quota(rates: list[TuitionRate], *, quota_amount, quota_currency: str | None
                     ) -> str | None:
    """If a rate shares the quota currency, say whether it exceeds the facilitation quota."""
    if quota_amount is None or not quota_currency:
        return None
    try:
        quota = Decimal(str(quota_amount))
    except Exception:
        return None
    same = [r for r in rates if (r.currency or "").upper() == quota_currency.upper()
            and not r.is_estimate]
    if not same:
        currencies = sorted({r.currency for r in rates if r.currency})
        if currencies:
            return (f"学费参考币种为 {'/'.join(currencies)}，与便利化额度币种 {quota_currency} 不同；"
                    "是否超额请按购汇当日折算后向银行确认（此处不做汇率估算）。")
        return None
    # Use the largest same-currency official amount as the conservative remittance planning figure.
    top = max(same, key=lambda r: r.amount)
    money = _format_money(top.amount, top.currency)
    quota_txt = _format_money(quota, quota_currency)
    if top.amount > quota:
        return (f"参考学费约 {money}，已高于年度便利化额度约 {quota_txt}；"
                "超额度办理时请按银行要求准备学费证明等材料。")
    return (f"参考学费约 {money}，未超过年度便利化额度约 {quota_txt}"
            "（仍以银行审核与缴费通知书为准）。")


TuitionResolver = Callable[..., TuitionContext]
