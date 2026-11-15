"""Read SCHOOL / PROGRAM / TUITION from Agent1 RDS for remittance advice.

All amounts and labels come from the working database — nothing is hardcoded
per school. Callers pass profile facts (school name, course, destination).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from decimal import Decimal
from typing import Any, Callable

from agent1.storage import read_session

# Profile / UI often stores SCHOOL.school_id / PROGRAM.program_id as the fact value.
_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)


def _looks_like_id(value: str | None) -> bool:
    return bool(value and _UUID_RE.match(value.strip()))

# Chinese display names for the catalogued schools (keys are casefolded
# catalogue names). Customer-facing copy reads 「香港大学」, not the English
# catalogue row.
SCHOOL_NAME_ZH = {
    "the chinese university of hong kong": "香港中文大学",
    "the hong kong university of science and technology": "香港科技大学",
    "the university of hong kong": "香港大学",
    "imperial college london": "帝国理工学院",
    "king's college london": "伦敦国王学院",
    "london school of economics and political science": "伦敦政治经济学院",
    "university college london": "伦敦大学学院",
    "university of cambridge": "剑桥大学",
    "university of edinburgh": "爱丁堡大学",
    "university of manchester": "曼彻斯特大学",
    "university of oxford": "牛津大学",
    "university of warwick": "华威大学",
    "boston university": "波士顿大学",
    "brown university": "布朗大学",
    "california institute of technology": "加州理工学院",
    "carnegie mellon university": "卡内基梅隆大学",
    "case western reserve university": "凯斯西储大学",
    "columbia university": "哥伦比亚大学",
    "cornell university": "康奈尔大学",
    "dartmouth college": "达特茅斯学院",
    "duke university": "杜克大学",
    "emory university": "埃默里大学",
    "georgia institute of technology": "佐治亚理工学院",
    "harvard university": "哈佛大学",
    "johns hopkins university": "约翰斯·霍普金斯大学",
    "massachusetts institute of technology": "麻省理工学院",
    "michigan state university": "密歇根州立大学",
    "new york university": "纽约大学",
    "northwestern university": "西北大学",
    "ohio state university": "俄亥俄州立大学",
    "penn state university park": "宾夕法尼亚州立大学帕克分校",
    "purdue university west lafayette": "普渡大学西拉法叶分校",
    "rice university": "莱斯大学",
    "stanford university": "斯坦福大学",
    "texas a&m university": "得克萨斯农工大学",
    "university of arizona": "亚利桑那大学",
    "university of california, berkeley": "加州大学伯克利分校",
    "university of california, davis": "加州大学戴维斯分校",
    "university of california, irvine": "加州大学尔湾分校",
    "university of california, los angeles": "加州大学洛杉矶分校",
    "university of california, san diego": "加州大学圣地亚哥分校",
    "university of california, santa barbara": "加州大学圣塔芭芭拉分校",
    "university of chicago": "芝加哥大学",
    "university of florida": "佛罗里达大学",
    "university of illinois at urbana-champaign": "伊利诺伊大学香槟分校",
    "university of maryland, college park": "马里兰大学帕克分校",
    "university of massachusetts amherst": "马萨诸塞大学阿默斯特分校",
    "university of michigan-ann arbor": "密歇根大学安娜堡分校",
    "university of minnesota": "明尼苏达大学",
    "university of north carolina at chapel hill": "北卡罗来纳大学教堂山分校",
    "university of pennsylvania": "宾夕法尼亚大学",
    "university of pittsburgh": "匹兹堡大学",
    "university of rochester": "罗切斯特大学",
    "university of southern california": "南加州大学",
    "university of texas at austin": "得克萨斯大学奥斯汀分校",
    "university of virginia": "弗吉尼亚大学",
    "university of washington": "华盛顿大学",
    "university of wisconsin-madison": "威斯康星大学麦迪逊分校",
    "vanderbilt university": "范德堡大学",
    "washington university in st louis": "圣路易斯华盛顿大学",
    "yale university": "耶鲁大学",
}


def school_display_name(name: str | None) -> str:
    """Chinese display name for a catalog school; English fallback."""
    if not name:
        return ""
    return SCHOOL_NAME_ZH.get(str(name).strip().casefold(), str(name).strip())

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

# Catalog rate_label strings → Chinese. Unmapped English labels are omitted
# from customer-facing lines rather than leaked into the answer.
RATE_LABEL_ZH = {
    "part time": "兼读制",
    "half time": "半时",
    "half time (htm)": "半时（HTM）",
    "half time (htmc)": "半时（HTMC）",
    "full time": "全日制",
    "full time (jsd)": "全日制（JSD）",
    "full time (ftmc)": "全日制（FTMC）",
    "stem": "STEM 院系",
    "non_stem": "非 STEM 院系",
    "summer": "暑期",
    "second year": "第二年",
    "second year: one term only": "第二年（仅一学期）",
    "second year (gsb)": "第二年（GSB）",
    "first year (gsb)": "第一年（GSB）",
    "g2 / second year": "G2/第二年",
    "tuition (g1)": "学费（G1）",
    "tuition (g2)": "学费（G2）",
    "tuition (all years)": "学费（全部学年）",
    "first three semesters": "首三学期",
    "subsequent semesters": "后续学期",
    "non-resident first 3 semesters": "非住校首三学期",
    "non-resident subsequent semesters": "非住校后续学期",
    "academic year incl. summer": "学年（含暑期）",
    "example 16-month total": "16 个月总计示例",
    "12 months": "12 个月",
    "18 months": "18 个月",
    "20 months": "20 个月",
    "regular enrollment": "常规注册",
    "reduced tuition": "减免学费",
    "continuing registration": "续读注册",
    "continuation fee": "续读费",
    "visiting researcher": "访问研究者",
    "intern": "实习",
    "clinical years 4-6": "临床年（第 4-6 年）",
    "pre-clinical years 1-3": "临床前（第 1-3 年）",
    "ddp 5th year": "双学位第五年",
    "sb thesis only tuition": "仅论文年学费",
    "light load tuition": "低学分学费",
    "marginal rate": "边际费率",
    "off-campus internship tuition": "校外实习学费",
    "six or fewer units": "6 学分及以下",
    "six or more units": "6 学分及以上",
    "8 units": "8 学分",
    "8-10 units": "8-10 学分",
    "9 units": "9 学分",
    "10 unit": "10 学分",
    "11 units": "11 学分",
    "11-18 units": "11-18 学分",
    "each graduate engineering unit above 18": "工程研究生超 18 学分（每学分）",
    "each graduate unit above 18": "研究生超 18 学分（每学分）",
    "each unit above the quarterly maximum": "超出季度上限学分（每学分）",
    "1 course rate": "1 门课费率",
    "2 course rate": "2 门课费率",
    "3 course rate": "3 门课费率",
    "4 course rate": "4 门课费率",
    "tuition for d.f.a. candidates in residence": "驻校 D.F.A. 候选人学费",
    "urop — over 18 units*": "UROP 超 18 学分",
    "1-7 units (summer only)": "1-7 学分（仅暑期）",
    "2025-26 intake": "2025-26 学年入学",
    "2026-27 intake": "2026-27 学年入学",
    "2017-18 to 2024-25 intakes": "2017-18 至 2024-25 学年入学",
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
    # Chinese display name (SCHOOL_NAME_ZH) for customer-facing copy.
    school_display: str | None = None
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
            "school_display": self.school_display,
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


# Method ids that mean "see the programme/office page" — they carry no actual
# channel information. When a level/programme row only lists these, the
# school-wide row (if any) has the real payment channels and must win.
_PLACEHOLDER_METHODS = {"programme_catalog_channels", "programme_page_channels",
                        "programme_specific", "hkust_registry_payment_channels"}


def _is_placeholder_payment_row(row) -> bool:
    methods = row["payment_methods"] or []
    return bool(methods) and all(m in _PLACEHOLDER_METHODS for m in methods)


def _merge_placeholder_payment(base_row, wide_row):
    """Keep the level/programme row's own billing rhythm, take the real
    channels from the school-wide row.

    Placeholder rows carry the degree-level billing_cycle / instalment facts
    that must survive the fallback; only the channel-ish fields were the
    placeholders (see HKUST: bachelor row says per_year_advance, the CSFU
    school-wide row lists the actual payment channels).
    """
    merged = dict(base_row)
    for key in ("payment_methods", "portal_name", "portal_url",
                "supports_multi_currency", "multi_currency_channels",
                "multi_currency_note"):
        merged[key] = wide_row[key]
    if base_row.get("accepts_credit_card") is None:
        merged["accepts_credit_card"] = wide_row.get("accepts_credit_card")
    return merged


def _load_payment_detail(con, *, school_id: str, program_id: str | None,
                         level: str | None) -> PaymentDetail | None:
    """Prefer programme override, then level, then school_wide.

    Placeholder level/programme rows (payment_methods are "see programme
    page" markers, portal points at a catalog page) never mask a
    substantive school-wide row: HKUST MSc, for example, falls back to the
    CSFU payment-methods row instead of the programme-catalog placeholder.
    The fallback merges channel fields from the school-wide row while
    keeping the degree row's billing_cycle / instalment facts.
    """
    row = None
    if program_id:
        hit = con.execute(
            """SELECT * FROM tuition_payment_detail
               WHERE school_id=%s AND scope='programme' AND program_id=%s
               ORDER BY recorded_at DESC NULLS LAST LIMIT 1""",
            (school_id, program_id),
        ).fetchone()
        if hit and not _is_placeholder_payment_row(hit):
            return _payment_from_row(hit)
        # placeholder or missing → keep looking at level / school_wide
    if level:
        hit = con.execute(
            """SELECT * FROM tuition_payment_detail
               WHERE school_id=%s AND scope='level' AND level=%s
               ORDER BY recorded_at DESC NULLS LAST LIMIT 1""",
            (school_id, level),
        ).fetchone()
        if hit and not _is_placeholder_payment_row(hit):
            return _payment_from_row(hit)
        row = hit  # may be None or a placeholder — decided against school_wide below
    # level/programme row is missing or a placeholder → prefer a substantive
    # school-wide row before settling for what we have.
    wide = con.execute(
        """SELECT * FROM tuition_payment_detail
           WHERE school_id=%s AND scope='school_wide'
           ORDER BY recorded_at DESC NULLS LAST LIMIT 1""",
        (school_id,),
    ).fetchone()
    if wide and not _is_placeholder_payment_row(wide):
        if row is None:
            return _payment_from_row(wide)
        if _is_placeholder_payment_row(row):
            return _payment_from_row(_merge_placeholder_payment(row, wide))
    return _payment_from_row(row) if row else None


def _zh_school_to_en(query: str) -> str:
    """Map a user-supplied Chinese school name to its English catalogue name."""
    q = query.strip()
    for en, zh in SCHOOL_NAME_ZH.items():
        if q == zh or zh in q:
            return en
    return query


def resolve_school_id(school_text) -> str | None:
    """Best-effort resolve a free-text school mention to its SCHOOL.school_id UUID.

    Used before writing or comparing the profile ``school`` field so that a
    Chinese/English name and the stored UUID compare equal. Returns the input
    unchanged when it already looks like an id, and None when no single
    catalogue row matches (callers then keep the original behaviour).
    """
    if school_text is None:
        return None
    text = str(school_text).strip()
    if not text:
        return None
    if _looks_like_id(text):
        return text
    try:
        with read_session() as con:
            schools = _find_schools(con, text, None)
            if len(schools) == 1:
                return schools[0]["school_id"]
    except Exception:  # noqa: BLE001 — RDS unavailable must not break profile writes
        return None
    return None


def _find_schools(con, school_query: str, destination_country: str | None):
    q = _zh_school_to_en(school_query).strip()
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


# Degree-level tokens: a query naming one level must never fuzzy-match a
# programme of another level ("Bachelor of Business Administration" vs
# "Master of Business Administration" shared 3 of 4 tokens and scored 45).
_UG_LEVEL_TOKENS = {"bachelor", "undergraduate", "ug", "bba", "bsc", "beng", "llb"}
_PG_LEVEL_TOKENS = {"master", "msc", "mba", "ma", "llm", "mphil", "mpa", "med", "msw",
                    "mar", "postgraduate", "graduate", "professional", "phd", "doctoral",
                    "edd", "dpa", "dnurs", "psyd", "mds", "emb"}
# Filler words that inflated token-overlap scores without carrying meaning.
_SCORE_STOPWORDS = {"of", "in", "the", "and", "at", "for"}


def _level_conflict(query_tokens: set[str], name_tokens: set[str]) -> bool:
    return bool((query_tokens & _UG_LEVEL_TOKENS and name_tokens & _PG_LEVEL_TOKENS)
                or (query_tokens & _PG_LEVEL_TOKENS and name_tokens & _UG_LEVEL_TOKENS))


def _score_program(name: str, level: str | None, course_q: str) -> int:
    n, c = _norm(name), _norm(course_q)
    if not c:
        return 0
    if n == c:
        return 100
    nt, ct = set(n.split()), set(c.split())
    if _level_conflict(ct, nt):
        return 0
    if c in n or n in c:
        return 80
    # token overlap (filler words excluded: "MSc in X" must match
    # "Master of Science in X" instead of being diluted by "of"/"in")
    nt, ct = nt - _SCORE_STOPWORDS, ct - _SCORE_STOPWORDS
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


def _norm_year(year: str | None) -> str:
    return (year or "").strip().replace("-", "/")


def _latest_year(rates) -> str | None:
    years = sorted({_norm_year(r.academic_year) for r in rates if r.academic_year})
    return years[-1] if years else None


def _ug_wide_rates(con, school_id: str) -> list:
    """School-level UG rows (bachelor/undergraduate pseudo-programmes),
    newest academic year only, deduped. Grouped rows (HKU's "Undergraduate
    programmes (STEM / Non-STEM Faculties)") get stem/non_stem rate labels so
    the answer line can name the faculty group."""
    rows = con.execute(
        """SELECT program_id, name FROM program
           WHERE school_id=%s AND level IN ('bachelor', 'undergraduate')""",
        (school_id,),
    ).fetchall()
    merged: list = []
    for p in rows:
        name_l = _norm(p["name"])
        for rate in _load_rates(con, p["program_id"]):
            label = rate.rate_label
            if "non-stem" in name_l or "non stem" in name_l:
                label = "non_stem"
            elif "stem" in name_l:
                label = "stem"
            if label != rate.rate_label:
                rate = replace(rate, rate_label=label)
            merged.append(rate)
    year = _latest_year(merged)
    if year:
        merged = [r for r in merged if _norm_year(r.academic_year) == year]
    seen, out = set(), []
    for r in merged:
        key = (r.fee_status, r.study_mode, r.amount, r.currency, r.rate_label)
        if key not in seen:
            seen.add(key)
            out.append(r)
    return out


def _refresh_ug_rates(con, ctx: TuitionContext) -> TuitionContext:
    """Undergraduate rows are often ingested per programme for an older year
    while the school publishes newer school-wide UG pricing (HKU: 2026/27
    programme rows vs the 2027/28 STEM/Non-STEM page). When the school-level
    rows are newer, quote those instead of the stale programme rows."""
    if not ctx.rates or ctx.program_level not in {"bachelor", "undergraduate"}:
        return ctx
    programme_year = _latest_year(ctx.rates)
    if not programme_year:
        return ctx
    wide = _ug_wide_rates(con, ctx.school_id)
    wide_year = _latest_year(wide)
    if not wide or not wide_year or wide_year <= programme_year:
        return ctx
    # Quote the newest school-wide UG pricing directly; the year switch itself
    # is an ingest detail users do not need to see.
    ctx.rates = wide
    return ctx


def _attach_payment(con, ctx: TuitionContext) -> TuitionContext:
    if not ctx.school_id:
        return ctx
    ctx = _refresh_ug_rates(con, ctx)
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
        ctx.school_display = school_display_name(school_row["name"])
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
                # Undergraduate queries fall back to the school's UG-level rows
                # (bachelor/undergraduate pseudo-programmes, newest year) —
                # taught-postgraduate fees vary per programme and must NOT be
                # merged, so the fallback stays undergraduate-only.
                cq_tokens = set(_norm(str(course)).split())
                if cq_tokens & _UG_LEVEL_TOKENS or "本科" in str(course):
                    wide = _ug_wide_rates(con, ctx.school_id)
                    if wide:
                        ctx.program_id = None
                        ctx.program_name = None
                        ctx.program_level = "undergraduate"
                        ctx.program_scope = "level"
                        ctx.rates = wide
                        ctx.match = "level"
                        ctx.note = (
                            f"未精确匹配到「{course}」的项目行，已按该校本科生"
                            f"{_latest_year(wide)} 学年公布学费整理"
                            "（分 STEM / 非 STEM 院系）；请以录取项目缴费通知为准。"
                        )
                        return _attach_payment(con, ctx)
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
    if rate.rate_label:
        label = RATE_LABEL_ZH.get(str(rate.rate_label).strip().casefold())
        if label and label not in bits:
            # Unmapped English labels stay out of the Chinese answer line.
            bits.append(label)
    head = " · ".join(bits) if bits else "学费参考"
    basis = FEE_BASIS_ZH.get(rate.fee_basis or "", rate.fee_basis or "")
    money = _format_money(rate.amount, rate.currency)
    if basis:
        money = f"{basis} {money}"
    est = "（估算，请以缴费通知为准）" if rate.is_estimate else ""
    return f"{head}：{money}{est}"


_LOCAL_FEE_STATUSES = {"home", "local", "in_state"}
_NON_LOCAL_FEE_STATUSES = {"non_local", "international", "out_of_state"}


def fee_status_tiers(rates: list[TuitionRate]) -> list[dict[str, Any]]:
    """Fee-tier options for the fee_status question, priced from the catalog.

    Returns [] when the school publishes a single tier (no question needed);
    otherwise one option per side: local / non-local with sample amounts.
    """
    official = [r for r in rates if not r.is_estimate]
    pool = official or rates
    # Price the tier options from the newest published year only — mixing
    # years into one range would conflate cohort prices with fee bands.
    year = _latest_year(pool)
    if year:
        pool = [r for r in pool if _norm_year(r.academic_year) == year]
    tiers: list[dict[str, Any]] = []
    for value, wanted in (("local", _LOCAL_FEE_STATUSES),
                          ("non_local", _NON_LOCAL_FEE_STATUSES)):
        hits = [r for r in pool if (r.fee_status or "") in wanted]
        if hits:
            amounts = sorted({r.amount for r in hits})
            if len(amounts) == 1:
                sample = _format_money(amounts[0], hits[0].currency)
            else:
                # One tier with several published prices (HKU: STEM / Non-STEM
                # UG bands) — quote the range instead of one misleading figure.
                low = _format_money(amounts[0], hits[0].currency)
                high = _format_money(amounts[-1], hits[-1].currency)
                low_tail = low.rsplit(" ", 1)
                high_tail = high.rsplit(" ", 1)
                if (len(low_tail) == 2 and len(high_tail) == 2
                        and low_tail[1] == high_tail[1]):
                    sample = f"{low_tail[0]}–{high_tail[0]} {low_tail[1]}"
                else:
                    sample = f"{low}–{high}"
            tiers.append({
                "value": value,
                "label": "本地生" if value == "local" else "非本地生",
                "sample": sample,
            })
    # Ask only when both sides exist.
    return tiers if len(tiers) == 2 else []


def filter_rates_by_fee_status(rates: list[TuitionRate], fee_status: str | None
                               ) -> list[TuitionRate]:
    """Keep only the rates on the user's fee tier; fall back to all rates."""
    if not fee_status:
        return rates
    wanted = (_LOCAL_FEE_STATUSES if str(fee_status).strip().casefold() in
              {"local", *_LOCAL_FEE_STATUSES} else _NON_LOCAL_FEE_STATUSES)
    kept = [r for r in rates if (r.fee_status or "") in wanted]
    return kept or rates


def compare_to_quota(rates: list[TuitionRate], *, quota_amount, quota_currency: str | None
                     ) -> str | None:
    """If a rate shares the quota currency, say whether it exceeds the facilitation quota.

    HKD tuition against a USD quota uses the official Linked Exchange Rate
    System band (7.75–7.85 HKD/USD) for a bounded estimate instead of
    inventing a spot rate: if the whole band is below (or above) the quota,
    the answer states the conclusion explicitly.
    """
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
        if not currencies:
            return None
        if quota_currency.upper() == "USD" and currencies == ["HKD"]:
            hkd_rates = [r for r in rates if r.currency == "HKD" and not r.is_estimate]
            top = max(r.amount for r in hkd_rates)
            quota_txt = _format_money(quota, quota_currency)
            # 7.75 gives the largest USD value, 7.85 the smallest — the whole
            # conversion band stays inside the official LERS range.
            usd_lo = (top / Decimal("7.85")).quantize(Decimal("1"))
            usd_hi = (top / Decimal("7.75")).quantize(Decimal("1"))
            money = _format_money(top, "HKD")
            if usd_hi <= quota:
                return (f"参考学费约 {money}，按港元联系汇率区间（7.75–7.85）估算约 "
                        f"{_format_money(usd_lo, 'USD')}–{_format_money(usd_hi, 'USD')}，"
                        f"未超过年度便利化额度约 {quota_txt}（以银行购汇当日折算为准）。")
            if usd_lo >= quota:
                return (f"参考学费约 {money}，按港元联系汇率区间（7.75–7.85）估算约 "
                        f"{_format_money(usd_lo, 'USD')}–{_format_money(usd_hi, 'USD')}，"
                        f"已超过年度便利化额度约 {quota_txt}；"
                        "超额度办理时请按银行要求准备学费证明等材料。")
            return (f"参考学费约 {money}，按港元联系汇率区间估算接近年度便利化额度约 "
                    f"{quota_txt}；是否超过请按购汇当日折算后与银行确认。")
        return (f"学费参考币种为 {'/'.join(currencies)}，与便利化额度币种 {quota_currency} 不同；"
                "是否超额请按购汇当日折算后向银行确认（此处不做汇率估算）。")
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
