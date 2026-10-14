"""M06 缺失条件判断与追问（docs/agent2-module-design.md M06，测试指标见 docs/agent2-m06-metrics.md）。

条件表由代码维护，模型不能临时发明必需字段：
- must_have：缺失就不能作确定结论（needs_input），先问这些；
- nice_to_have：不追问，只在回答中注明假设缺口；
- 知识条目的适用条件字段（condition_fields）：主题必需字段齐全后，才对影响具体材料结论的
  未知字段追问。
每轮最多 MAX_QUESTIONS_PER_ROUND 个问题；已确认字段不追问；Profile 中 status=unknown 的值
一律视为未知（即使存了值）；用户在会话中说的条件只作临时条件（source=conversation），
不当作已确认档案。
"""
import re
import time
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation

from .models import FollowUpQuestion, Notice, Profile

MAX_QUESTIONS_PER_ROUND = 2


def _enum(*values):
    return lambda v: None if v in values else f"取值必须是 {' / '.join(values)} 之一"


def _text(v):
    return None if isinstance(v, str) and v.strip() else "必须是非空文本"


def _iso_date(v):
    try:
        date.fromisoformat(v)
        return None
    except (TypeError, ValueError):
        return "必须是 YYYY-MM-DD 日期"


def _age(v):
    return None if isinstance(v, int) and not isinstance(v, bool) and 0 < v < 120 else "必须是 1–119 的整数"


def _bool(v):
    return None if isinstance(v, bool) else "必须是 是/否"


def _amount(v):
    try:
        return None if Decimal(str(v)) > 0 else "必须大于 0"
    except InvalidOperation:
        return "必须是十进制金额"


def _currency(v):
    return None if isinstance(v, str) and re.fullmatch(r"[A-Z]{3}", v) else "必须是 ISO 4217 三字母币种"


# field_code -> (中文名, 校验, question_code, 追问文本)
FIELDS = {
    "role": ("角色", _enum("student", "parent"), "ask_role", "你是学生本人还是家长？"),
    "destination_country": ("留学目的地", _enum("UK", "US", "CN-HK"), "ask_destination_country",
                            "你计划去哪个国家/地区留学？（英国 / 美国 / 香港）"),
    "visa_category": ("签证类别", _enum("student_visa", "visitor_visa", "graduate_visa", "dependant_visa"),
                      "ask_visa_category", "你要办理哪一类签证？例如学生签证（英国 Student visa）或访问签证。"),
    "school": ("学校", _text, "ask_school", "你被哪所学校录取（或计划申请哪所学校）？"),
    "offer_status": ("offer 状态", _enum("none", "conditional", "unconditional"), "ask_offer_status",
                     "你目前拿到的是无条件 offer、有条件 offer，还是还没有 offer？"),
    "planned_start_date": ("预计入学时间", _iso_date, "ask_planned_start_date", "课程预计哪天开学？"),
    "applicant_age": ("申请人年龄", _age, "ask_applicant_age", "申请人现在多大？（是否未满 18 岁会影响材料要求）"),
    "funding_source": ("资金来源", _enum("own_or_parents", "student_loan", "sponsor", "scholarship"),
                       "ask_funding_source",
                       "学费和生活费的资金来自哪里？本人或父母存款、学生贷款，还是机构资助/奖学金？"),
    "tb_listed_country_resident": ("是否居住在需做肺结核检测的国家", _bool, "ask_tb_listed_country_resident",
                                   "申请前你是否在需要做肺结核（TB）检测的国家或地区（如中国大陆）居住？"),
    "remittance_purpose": ("汇款用途", _enum("tuition", "living"), "ask_remittance_purpose",
                           "这笔汇款是交学费还是生活费？"),
    "payment_deadline": ("付款期限", _iso_date, "ask_payment_deadline", "学校要求的付款截止日期是哪天？"),
    "amount": ("金额", _amount, "ask_amount", "需要汇多少钱？"),
    "currency": ("币种", _currency, "ask_currency", "金额是什么币种？（如 GBP、USD、HKD）"),
    "payer": ("付款人", _enum("self", "parent", "other"), "ask_payer", "由谁付款？本人、父母还是其他人？"),
    "account_region": ("开户地区", _enum("UK", "US", "CN-HK", "CN"), "ask_account_region",
                       "你想在哪个地区开汇丰账户？"),
    "account_product": ("账户产品", _text, "ask_account_product", "想开哪种账户或产品？"),
    "applicant_identity": ("申请人身份", _enum("student", "parent", "other"), "ask_applicant_identity",
                           "开户/申请人是学生本人、家长还是其他人？"),
    "card_product_scope": ("信用卡产品范围", _text, "ask_card_product_scope", "你想比较哪些信用卡产品？"),
    "application_region": ("申请地区", _enum("UK", "US", "CN-HK", "CN"), "ask_application_region",
                           "你打算在哪个地区申请信用卡？"),
}


BOOLEAN_FIELDS = frozenset(code for code, spec in FIELDS.items() if spec[1] is _bool)


@dataclass(frozen=True)
class TopicRule:
    must_have: tuple
    nice_to_have: tuple = ()


# 设计 M06 表：作确定性结论前必须确认的条件
CONDITION_TABLE = {
    "visa_documents": TopicRule(("destination_country", "visa_category"), ("school", "planned_start_date")),
    "remittance_tuition": TopicRule(("destination_country", "school", "remittance_purpose", "payment_deadline"),
                                    ("amount", "currency")),
    "remittance_living": TopicRule(("destination_country", "remittance_purpose", "payer")),
    "hsbc_account": TopicRule(("account_region", "account_product", "applicant_identity")),
    "overseas_spending": TopicRule(("destination_country",)),
    "credit_card": TopicRule(("card_product_scope", "application_region", "applicant_age", "applicant_identity")),
}


@dataclass
class Fact:
    value: object
    source: str  # "profile"（已确认档案）| "conversation"（本会话临时条件，未写入档案）


@dataclass
class Assessment:
    topic: str
    state: str  # ready | needs_input | invalid_input | field_conflict | unsupported_topic
    explanation: str
    profile_version: int
    facts: dict = field(default_factory=dict)
    missing_fields: list = field(default_factory=list)
    pending_fields: list = field(default_factory=list)
    assumption_gaps: list = field(default_factory=list)
    questions: list = field(default_factory=list)
    notices: list = field(default_factory=list)
    rule_ms: float = 0.0
    model_ms: float = 0.0

    @property
    def ready(self):
        return self.state == "ready"


def label(field_code):
    return FIELDS[field_code][0] if field_code in FIELDS else field_code


def validate(field_code, value):
    if field_code not in FIELDS:
        return "不是已登记的档案字段"
    return FIELDS[field_code][1](value)


def _question(field_code, rule, version, text=None):
    _, _, code, default = FIELDS[field_code]
    return FollowUpQuestion(question_code=code, field=field_code, text=text or default,
                            rule=rule, profile_version=version)


def assess(topic, profile: Profile, stated=None, *, condition_fields=(), expected_profile_version=None,
           suggest=None):
    """Decide whether this round can conclude, and which (at most two) questions to ask.

    stated: conditions the user gave in this conversation (not saved to the Profile).
    condition_fields: Profile fields that knowledge requirements depend on (from M08).
    suggest: optional model hook returning field codes in preferred order; the server
    keeps only fields that this table already considers missing/pending. Its wait time
    is reported as model_ms, separate from rule_ms.
    """
    started = time.perf_counter()
    version = profile.version
    notices = []
    rule = CONDITION_TABLE.get(topic)
    if rule is None:
        return Assessment(topic, "unsupported_topic", f"主题 {topic} 没有条件表，不作确定结论",
                          version, rule_ms=(time.perf_counter() - started) * 1000)
    if expected_profile_version is not None and expected_profile_version != version:
        notices.append(Notice(code="profile_version_changed",
                              message=f"档案已从 v{expected_profile_version} 更新为 v{version}，"
                                      f"本轮按 v{version} 的已确认字段重新判断。"))

    facts, invalid, conflicts = {}, [], []
    for code, item in profile.fields.items():
        if item.status != "confirmed" or item.value is None:
            continue
        error = validate(code, item.value)
        if error:
            invalid.append((code, item.value, error))
        else:
            facts[code] = Fact(item.value, "profile")
    for code, value in (stated or {}).items():
        if value is None:
            continue
        error = validate(code, value)
        if error:
            invalid.append((code, value, error))
        elif code in facts and facts[code].source == "profile":
            if facts[code].value != value:
                conflicts.append((code, facts[code].value, value))
        else:
            facts[code] = Fact(value, "conversation")
    for code, _, _ in invalid:
        facts.pop(code, None)
    for code, _, _ in conflicts:
        facts.pop(code, None)

    conflicted = {code for code, _, _ in conflicts}
    missing = [f for f in rule.must_have if f not in facts and f not in conflicted]
    candidates = [(code, f"invalid.{code}", None) for code, _, _ in invalid]
    candidates += [(code, f"conflict.{code}",
                    f"档案中{label(code)}是「{old}」，这次你提到「{new}」。以哪一个为准？"
                    f"如果要改档案，请在档案页确认更新。") for code, old, new in conflicts]
    taken = {code for code, _, _ in candidates}
    candidates += [(code, f"{topic}.must_have.{code}", None) for code in missing if code not in taken]
    pending = []
    if not candidates:
        pending = list(dict.fromkeys(f for f in condition_fields if f in FIELDS and f not in facts))
        candidates = [(code, f"requirement_condition.{code}", None) for code in pending]

    model_ms = 0.0
    if suggest is not None and candidates:
        model_started = time.perf_counter()
        suggested = list(suggest(topic, [c[0] for c in candidates]) or [])
        model_ms = (time.perf_counter() - model_started) * 1000
        eligible = {c[0]: c for c in candidates}
        rejected = [f for f in suggested if f not in eligible]
        if rejected:
            notices.append(Notice(code="suggestion_rejected",
                                  message=f"模型建议追问的字段不在本轮条件表内，已忽略：{', '.join(rejected)}"))
        preferred = [eligible[f] for f in dict.fromkeys(suggested) if f in eligible]
        candidates = preferred + [c for c in candidates if c not in preferred]
    questions = [_question(code, rule_id, version, text)
                 for code, rule_id, text in candidates[:MAX_QUESTIONS_PER_ROUND]]

    if invalid:
        state = "invalid_input"
        explanation = "；".join(f"{label(c)}「{v}」{e}" for c, v, e in invalid)
    elif conflicts:
        state = "field_conflict"
        explanation = "；".join(f"{label(c)}：档案为「{o}」，本次消息为「{n}」" for c, o, n in conflicts)
    elif missing:
        state = "needs_input"
        explanation = "作确定结论前还需确认：" + "、".join(label(c) for c in missing)
    else:
        state = "ready"
        explanation = ("主题必需条件已齐全" + (
            "；部分材料取决于：" + "、".join(label(c) for c in pending) if pending else ""))
    missing_fields = list(dict.fromkeys(
        missing + [c for c, _, _ in invalid if c in rule.must_have]))
    return Assessment(
        topic=topic, state=state, explanation=explanation, profile_version=version, facts=facts,
        missing_fields=missing_fields, pending_fields=pending,
        assumption_gaps=[f for f in rule.nice_to_have if f not in facts],
        questions=questions, notices=notices,
        rule_ms=(time.perf_counter() - started) * 1000 - model_ms, model_ms=model_ms)
