"""M06 缺失条件判断与追问（docs/agent2-module-design.md M06，测试指标见 docs/agent2-m06-metrics.md）。

条件表由代码维护，模型不能临时发明必需字段：
- must_have：缺失就不能作确定结论（needs_input），先问这些；
- nice_to_have：不追问，只在回答中注明假设缺口；
- 知识条目的适用条件字段（condition_fields）：主题必需字段齐全后，才对影响具体材料结论的
  未知字段追问。
每轮最多 1 个追问（MAX_QUESTIONS_PER_ROUND）；已确认字段不追问；Profile 中 status=unknown
的值一律视为未知（即使存了值）；用户在会话中说的条件只作临时条件（source=conversation），
不当作已确认档案。
"""
import re
import time
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation

from .models import FollowUpQuestion, Notice, Profile

MAX_QUESTIONS_PER_ROUND = 1


def _enum(*values):
    def check(v):
        return None if v in values else f"取值必须是 {' / '.join(values)} 之一"
    check.values = values
    return check


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


def _multi_enum(*values, max_items=None):
    allowed = set(values)
    def check(v):
        if not isinstance(v, list) or not v:
            return "必须至少选择一项"
        if max_items is not None and len(v) > max_items:
            return f"最多选择 {max_items} 项"
        return None if len(v) == len(set(v)) and set(v) <= allowed else "包含未登记或重复的选项"
    check.values = values
    check.multi = True
    check.max_items = max_items
    return check


def _income_band_hkd(v):
    return None if isinstance(v, str) and re.fullmatch(r"(?:lt|gte|range)_\d+(?:_\d+)?", v) \
        else "收入区间格式不正确"


# field_code -> (中文名, 校验, question_code, 追问文本)
FIELDS = {
    "role": ("角色", _enum("student", "parent"), "ask_role", "你是学生本人还是家长？"),
    "beneficiary": ("服务对象", _text, "ask_beneficiary", "这份方案是为谁准备的？（学生姓名）"),
    "course": ("课程 / 学位", _text, "ask_course", "就读什么课程或学位？"),
    "destination_country": ("留学目的地", _enum("UK", "US", "CN-HK"), "ask_destination_country",
                            "你计划去哪个国家/地区留学？（英国 / 美国 / 香港）"),
    "visa_category": ("签证类别", _enum("student_visa", "visitor_visa", "graduate_visa", "dependant_visa"),
                      "ask_visa_category", "你要办理哪一类签证？例如学生签证（英国 Student visa）或访问签证。"),
    "school": ("学校", _text, "ask_school", "你被哪所学校录取（或计划申请哪所学校）？"),
    "offer_status": ("offer 状态", _enum("none", "conditional", "unconditional"), "ask_offer_status",
                     "你目前拿到的是无条件 offer、有条件 offer，还是还没有 offer？"),
    "planned_start_date": ("预计入学时间", _iso_date, "ask_planned_start_date", "课程预计哪天开学？"),
    "applicant_age": ("申请人年龄", _age, "ask_applicant_age", "申请人现在多大？（是否未满 18 岁会影响材料要求）"),
    "applicant_age_band": ("年龄区间", _enum("under_18", "18_or_over"), "ask_applicant_age_band",
                           "你目前是否已满 18 岁？"),
    "funding_source": ("资金来源", _enum("own_or_parents", "student_loan", "sponsor", "scholarship"),
                       "ask_funding_source",
                       "学费和生活费的资金来自哪里？本人或父母存款、学生贷款，还是机构资助/奖学金？"),
    "tb_listed_country_resident": ("是否居住在需做肺结核检测的国家", _bool, "ask_tb_listed_country_resident",
                                   "你是否在需要做肺结核（TB）检测的国家或地区（如中国大陆）住满 6 个月，"
                                   "且最近 6 个月内仍住在那里？"),
    "stay_over_6_months": ("是否在目的地停留 6 个月或以上", _bool, "ask_stay_over_6_months",
                           "这次去英国是否停留 6 个月或以上？"),
    "applicant_residence": ("申请人常住地", _enum("CN", "MO", "TW", "other"), "ask_applicant_residence",
                            "你目前常住在哪里？中国内地、澳门、台湾，还是其他地区？"),
    "hk_sponsor": ("香港本地保证人", _enum("institution", "individual"), "ask_hk_sponsor",
                   "你在香港的本地保证人是录取你的院校，还是个人？"),
    "hk_sponsor_permanent_resident": ("保证人是否香港永久性居民", _bool, "ask_hk_sponsor_permanent_resident",
                                      "你的个人保证人是香港永久性居民吗？"),
    "has_hk_identity_card": ("是否持有香港身份证", _bool, "ask_has_hk_identity_card", "你本人是否持有香港身份证？"),
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
    "annual_income_hkd": ("税前年收入（港币）", _amount, "ask_annual_income_hkd",
                          "你的税前年收入大约是多少港币？可以填估算金额。"),
    "annual_income_band_hkd": ("税前年收入区间（港币）", _income_band_hkd,
                               "ask_annual_income_band_hkd", "请选择税前年收入所在区间。"),
    "asset_band": ("可证明资产区间", _enum("none", "under_100k", "100k_500k", "500k_plus"),
                   "ask_asset_band", "你能否提供最近3个月的银行资产证明？可证明资产大致在哪个区间？"),
    "repayment_mode": ("还款方式", _enum("full", "sometimes_carry", "usually_carry"),
                       "ask_repayment_mode", "你通常会每月全额还款，还是可能分期或保留余额？"),
    "spending_scenarios": ("主要消费场景",
                           _multi_enum("online", "dining", "travel", "overseas", "mainland_china",
                                       "tuition", "retail", max_items=3),
                           "ask_spending_scenarios", "请选择最主要的两到三个消费场景。"),
    "reward_preference": ("奖励偏好", _enum("cashback", "points", "miles", "no_preference"),
                          "ask_reward_preference", "你更偏好返现、积分、里程，还是都可以？"),
    "monthly_card_spend_band_hkd": (
        "每月信用卡消费区间", _enum("under_2k", "2k_5k", "5k_10k", "10k_20k", "20k_plus"),
        "ask_monthly_card_spend_band_hkd", "你预计每月信用卡消费大约在哪个区间？"),
    "mainland_payment_method": (
        "内地消费付款方式", _enum("mobile_or_qr", "physical_card", "mixed_or_unsure"),
        "ask_mainland_payment_method", "你在中国内地消费时通常怎样付款？"),
    "full_time_student": ("是否全日制学生", _bool, "ask_full_time_student", "你目前是全日制大专或大学生吗？"),
    "student_institution_eligible": ("是否就读 HSBC 指定院校", _bool, "ask_student_institution_eligible",
                                     "你是否就读于 HSBC 指定名单内的香港全日制院校课程？"),
    "premier_relationship": ("是否为卓越理财客户", _bool, "ask_premier_relationship",
                             "你目前是否持有符合条件的 HSBC Premier 卓越理财关系？"),
    "hsbc_relationship_balance_band": (
        "HSBC 总关系结余区间", _enum("under_100k", "100k_999k", "1m_plus"),
        "ask_hsbc_relationship_balance_band", "请选择你在 HSBC 的总关系结余区间。"),
    "hsbc_premier_path": (
        "HSBC Premier 资格路径",
        _enum("existing_premier", "can_qualify_with_1m_trb", "not_applicable"),
        "ask_hsbc_premier_path", "请选择与你相符的 HSBC Premier 情况。"),
    "hk_resident": ("是否为香港居民", _bool, "ask_hk_resident",
                    "你目前是否为香港特别行政区居民？"),
}


BOOLEAN_FIELDS = frozenset(code for code, spec in FIELDS.items() if spec[1] is _bool)

OPTION_LABELS = {
    "role": {"student": "学生本人", "parent": "家长"},
    "destination_country": {"UK": "英国", "US": "美国", "CN-HK": "中国香港"},
    "visa_category": {"student_visa": "学生签证", "visitor_visa": "访问签证", "graduate_visa": "毕业生签证",
                      "dependant_visa": "陪读 / 家属签证"},
    "offer_status": {"none": "还没有 offer", "conditional": "有条件 offer", "unconditional": "无条件 offer"},
    "funding_source": {"own_or_parents": "本人或父母存款", "student_loan": "学生贷款", "sponsor": "机构资助",
                       "scholarship": "奖学金"},
    "remittance_purpose": {"tuition": "学费", "living": "生活费"},
    "payer": {"self": "本人", "parent": "父母", "other": "其他"},
    "account_region": {"UK": "英国", "US": "美国", "CN-HK": "中国香港", "CN": "中国内地"},
    "applicant_identity": {"student": "学生本人", "parent": "家长", "other": "其他"},
    "application_region": {"UK": "英国", "US": "美国", "CN-HK": "中国香港", "CN": "中国内地"},
    "applicant_age_band": {"under_18": "18 岁以下", "18_or_over": "18 岁或以上"},
    "asset_band": {"none": "目前无法提供银行资产证明", "under_100k": "可证明资产少于 10 万",
                   "100k_500k": "可证明资产 10–50 万", "500k_plus": "可证明资产 50 万以上"},
    "repayment_mode": {"full": "每月全额还款", "sometimes_carry": "偶尔分期或保留余额",
                       "usually_carry": "通常会保留余额"},
    "reward_preference": {"cashback": "返现", "points": "积分", "miles": "里程",
                          "no_preference": "都可以"},
    "monthly_card_spend_band_hkd": {
        "under_2k": "少于 HK$2,000",
        "2k_5k": "HK$2,000–4,999",
        "5k_10k": "HK$5,000–9,999",
        "10k_20k": "HK$10,000–19,999",
        "20k_plus": "HK$20,000 或以上",
    },
    "mainland_payment_method": {
        "mobile_or_qr": "主要使用手机支付或银联二维码",
        "physical_card": "主要刷实体卡",
        "mixed_or_unsure": "两者差不多，或暂不确定",
    },
    "hsbc_relationship_balance_band": {
        "under_100k": "少于 HK$100,000",
        "100k_999k": "HK$100,000–999,999",
        "1m_plus": "HK$1,000,000 或以上",
    },
    "hsbc_premier_path": {
        "existing_premier": "已经是 HSBC Premier 客户，并可满足信用卡关系结余要求",
        "can_qualify_with_1m_trb": "可在 HSBC 维持 HK$1,000,000 总关系结余，并愿意先升级 Premier",
        "not_applicable": "以上均不适用，或不考虑 Premier",
    },
    "spending_scenarios": {"online": "网购", "dining": "餐饮", "travel": "旅行",
                           "overseas": "境外消费", "mainland_china": "内地消费",
                           "tuition": "学费", "retail": "日常零售"},
    "applicant_residence": {"CN": "中国内地", "MO": "澳门", "TW": "台湾", "other": "其他地区"},
    "hk_sponsor": {"institution": "录取院校", "individual": "个人"},
}


def field_catalog():
    """Field metadata for clients: label, input kind, options and the follow-up question."""
    kinds = {_text: "text", _iso_date: "date", _age: "age", _bool: "bool", _amount: "amount",
             _currency: "currency"}
    catalog = {}
    for code, (label_zh, check, question_code, question) in FIELDS.items():
        values = getattr(check, "values", None)
        options = [{"value": v, "label": OPTION_LABELS.get(code, {}).get(v, v)} for v in values] if values else \
            [{"value": True, "label": "是"}, {"value": False, "label": "否"}] if check is _bool else []
        kind = "multi_enum" if getattr(check, "multi", False) else "enum" if values else kinds.get(check, "text")
        catalog[code] = {"label": label_zh, "kind": kind, "options": options, "question": question,
                         "max_selections": getattr(check, "max_items", None)}
    return catalog


FINANCIAL_FIELDS = frozenset({"annual_income_hkd", "annual_income_band_hkd", "asset_band",
                              "hsbc_relationship_balance_band", "hsbc_premier_path",
                              "monthly_card_spend_band_hkd", "repayment_mode", "amount"})
PERSONAL_FIELDS = frozenset({"applicant_age", "applicant_age_band", "residence_region",
                             "applicant_residence", "hk_resident", "full_time_student",
                             "student_institution_eligible"})
VOLATILE_FIELDS = {"annual_income_hkd": 365, "annual_income_band_hkd": 365, "asset_band": 365,
                   "repayment_mode": 365, "monthly_card_spend_band_hkd": 365,
                   "mainland_payment_method": 365,
                   "full_time_student": 365, "student_institution_eligible": 365,
                   "premier_relationship": 180, "hsbc_relationship_balance_band": 180,
                   "hsbc_premier_path": 180, "hk_resident": 365, "offer_status": 90}


def field_definitions():
    """Versioned registry persisted by the service for audit and UI rendering."""
    result = {}
    for code, item in field_catalog().items():
        domains = [name for name, rule in CONDITION_TABLE.items()
                   if code in (*rule.must_have, *rule.nice_to_have)] if "CONDITION_TABLE" in globals() else []
        if code in {"application_region", "applicant_age_band", "annual_income_band_hkd", "asset_band",
                    "repayment_mode", "spending_scenarios", "reward_preference", "full_time_student",
                    "student_institution_eligible",
                    "premier_relationship", "hsbc_relationship_balance_band", "hsbc_premier_path",
                    "monthly_card_spend_band_hkd", "mainland_payment_method", "hk_resident"}:
            domains.append("credit_card")
        result[code] = {**item, "domains": sorted(set(domains)),
                        "sensitivity": "financial" if code in FINANCIAL_FIELDS else
                                       "personal" if code in PERSONAL_FIELDS else "standard",
                        "freshness_days": VOLATILE_FIELDS.get(code), "ask_priority": 0,
                        "ask_only_when_needed": True}
    return result


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
    item = field_catalog()[field_code]
    input_type = "multi_select" if item["kind"] == "multi_enum" else \
        "single_select" if item["options"] else item["kind"] if item["kind"] in ("date",) else "text"
    # Date and option answers come from an explicit follow-up control; save them
    # immediately so the same question is not asked again next turn.
    persist = "save_on_select" if item["options"] or item["kind"] == "date" else "proposal"
    return FollowUpQuestion(question_code=code, field=field_code, text=text or default,
                            rule=rule, profile_version=version, input_type=input_type,
                            options=item["options"], max_selections=item["max_selections"],
                            persist_mode=persist)


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
