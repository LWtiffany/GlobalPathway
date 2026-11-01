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


def _residency_years(v):
    return None if isinstance(v, (int, float)) and not isinstance(v, bool) and 0 <= v < 120 \
        else "必须是 0–119 的居住年数"


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
    "role": ("角色", _enum("student", "parent"), "ask_role", "您是学生本人还是家长？"),
    "beneficiary": ("服务对象", _text, "ask_beneficiary", "这份方案是为谁准备的？（学生姓名）"),
    "course": ("课程 / 学位", _text, "ask_course", "就读什么课程或学位？"),
    "destination_country": ("留学目的地", _enum("UK", "US", "CN-HK"), "ask_destination_country",
                            "您计划去哪个国家/地区留学？（英国 / 美国 / 香港）"),
    "visa_category": ("签证类别", _enum("student_visa", "visitor_visa", "graduate_visa", "dependant_visa"),
                      "ask_visa_category", "您要办理哪一类签证？例如学生签证或访问签证。"),
    "school": ("学校", _text, "ask_school", "您被哪所学校录取（或计划申请哪所学校）？"),
    "offer_status": ("offer 状态", _enum("none", "conditional", "unconditional"), "ask_offer_status",
                     "您目前拿到的是无条件 offer、有条件 offer，还是还没有 offer？"),
    "planned_start_date": ("预计入学时间", _iso_date, "ask_planned_start_date", "课程预计哪天开学？"),
    "applicant_age": ("申请人年龄", _age, "ask_applicant_age", "申请人现在多大？"),
    "applicant_age_band": ("年龄区间", _enum("under_18", "18_or_over"), "ask_applicant_age_band",
                           "您目前是否已满 18 岁？"),
    "funding_source": ("资金来源", _enum("own_or_parents", "student_loan", "sponsor", "scholarship"),
                       "ask_funding_source",
                       "学费和生活费的资金来自哪里？本人或父母存款、学生贷款，还是机构资助/奖学金？"),
    "tb_listed_country_resident": ("是否居住在需做肺结核检测的国家", _bool, "ask_tb_listed_country_resident",
                                   "您是否在需要做肺结核（TB）检测的国家或地区（如中国大陆）住满 6 个月，"
                                   "且最近 6 个月内仍住在那里？"),
    "stay_over_6_months": ("是否在目的地停留 6 个月或以上", _bool, "ask_stay_over_6_months",
                           "这次去英国是否停留 6 个月或以上？"),
    "applicant_residence": ("申请人常住地", _enum("CN", "MO", "TW", "other"), "ask_applicant_residence",
                            "您目前常住在哪里？中国内地、澳门、台湾，还是其他地区？"),
    "hk_sponsor": ("香港本地保证人", _enum("institution", "individual"), "ask_hk_sponsor",
                   "您在香港的本地保证人是录取您的院校，还是个人？"),
    "hk_sponsor_permanent_resident": ("保证人是否香港永久性居民", _bool, "ask_hk_sponsor_permanent_resident",
                                      "您的个人保证人是香港永久性居民吗？"),
    "has_hk_identity_card": ("是否持有香港身份证", _bool, "ask_has_hk_identity_card", "您本人是否持有香港身份证？"),
    "remittance_purpose": ("汇款用途", _enum("tuition", "living"), "ask_remittance_purpose",
                           "这笔汇款是交学费还是生活费？"),
    "payment_deadline": ("付款期限", _iso_date, "ask_payment_deadline", "学校要求的付款截止日期是哪天？"),
    "amount": ("金额", _amount, "ask_amount", "需要汇多少钱？"),
    "currency": ("币种", _currency, "ask_currency", "金额是什么币种？（如 GBP、USD、HKD）"),
    "payer": ("付款人", _enum("self", "parent", "other"), "ask_payer", "由谁付款？本人、父母还是其他人？"),
    "account_region": ("开户地区", _enum("UK", "US", "CN-HK", "CN"), "ask_account_region",
                       "您想在哪个地区开账户？选一个就好，我会按当地规则为您整理。"),
    "account_product": ("账户产品", _text, "ask_account_product",
                        "您已经有想了解的账户了吗？有的话告诉我名称；还没决定也没关系。"),
    "applicant_identity": ("申请人身份", _enum("student", "parent", "other"), "ask_applicant_identity",
                           "这次实际开户的是学生本人、家长，还是其他人？"),
    "account_applicant_relation": ("开户人与提问者关系", _enum("self", "child", "parent"),
                                   "ask_account_applicant_relation", "这次实际开户的是您本人、孩子，还是家长？"),
    "uk_residency_years": ("英国居住年数", _residency_years, "ask_uk_residency_years",
                           "实际开户人在英国或指定属地居住多久了？"),
    "application_location": ("开户申请时所在地", _enum("CN", "UK", "US", "CN-HK", "other"),
                             "ask_application_location", "准备申请时，实际开户人会在哪里？"),
    "identity_document_type": ("开户身份证件", _enum("hkid", "passport", "eep", "other"),
                               "ask_identity_document_type", "开户人准备使用哪种证件：香港身份证、护照，还是港澳通行证？"),
    "qualifying_course_offer": ("合格课程录取证明", _bool, "ask_qualifying_course_offer",
                                "开户人有符合该账户条件的课程录取证明吗？"),
    "tax_certificate_or_ssn": ("SSN 或有效税务证明", _bool, "ask_tax_certificate_or_ssn",
                               "开户人有 SSN 或可提交有效税务证明吗？"),
    "guardian_present": ("家长或监护人陪同", _bool, "ask_guardian_present",
                         "未成年开户人到网点时会有家长或监护人陪同吗？"),
    "existing_hsbc_hk_customer": ("是否已有香港汇丰客户关系", _bool,
                                  "ask_existing_hsbc_hk_customer",
                                  "开户人目前是否已经是香港汇丰客户？"),
    "card_product_scope": ("信用卡产品范围", _text, "ask_card_product_scope", "您想比较哪些信用卡？"),
    "want_credit_card": (
        "是否想办信用卡", _enum("yes", "no"), "ask_want_credit_card",
        "信用卡不是硬性必需。您这次想继续看看可申请的卡吗？"),
    "application_region": ("申请地区", _enum("UK", "US", "CN-HK", "CN"), "ask_application_region",
                           "您打算在哪个地区申请信用卡？"),
    "annual_income_hkd": ("税前年收入（港币）", _amount, "ask_annual_income_hkd",
                          "您的税前年收入大约是多少港币？填估算金额就可以。"),
    "annual_income_band_hkd": ("税前年收入区间（港币）", _income_band_hkd,
                               "ask_annual_income_band_hkd", "请选择您的税前年收入区间。"),
    "asset_band": ("可证明资产区间", _enum("none", "under_100k", "100k_500k", "500k_plus"),
                   "ask_asset_band", "您能提供最近 3 个月的银行资产证明吗？大致在哪个区间？"),
    "repayment_mode": ("还款方式", _enum("full", "sometimes_carry", "usually_carry"),
                       "ask_repayment_mode", "您通常会每月全额还款，还是可能分期或保留余额？"),
    "spending_scenarios": ("主要消费场景",
                           _multi_enum("online", "dining", "travel", "overseas", "mainland_china",
                                       "tuition", "retail", "groceries", max_items=3),
                           "ask_spending_scenarios", "请选择最常见的两到三个消费场景。"),
    "study_card_focus": ("信用卡选择侧重",
                         _enum("lower_fee", "travel_benefits", "compare"),
                         "ask_study_card_focus", "您更看重较低年费，还是旅行权益？"),
    "reward_preference": ("奖励偏好", _enum("cashback", "points", "miles", "no_preference"),
                          "ask_reward_preference", "您更偏好返现、积分、里程，还是都可以？"),
    "monthly_card_spend_band_hkd": (
        "每月信用卡消费区间", _enum("under_2k", "2k_5k", "5k_10k", "10k_20k", "20k_plus"),
        "ask_monthly_card_spend_band_hkd", "您预计每月信用卡消费大约在哪个区间？"),
    "mainland_payment_method": (
        "内地消费付款方式", _enum("mobile_or_qr", "physical_card", "mixed_or_unsure"),
        "ask_mainland_payment_method", "您在中国内地消费时通常怎样付款？"),
    "full_time_student": ("是否全日制学生", _bool, "ask_full_time_student", "您目前是全日制大专或大学生吗？"),
    "student_institution_eligible": ("是否就读汇丰指定院校", _bool, "ask_student_institution_eligible",
                                     "您是否就读于汇丰指定名单内的香港全日制院校课程？"),
    "premier_relationship": ("是否为卓越理财客户", _bool, "ask_premier_relationship",
                             "您目前是否持有符合条件的汇丰卓越理财关系？"),
    "hsbc_relationship_balance_band": (
        "汇丰总关系结余区间", _enum("under_100k", "100k_999k", "1m_plus"),
        "ask_hsbc_relationship_balance_band", "请选择您在汇丰的总关系结余区间。"),
    "hsbc_premier_path": (
        "汇丰卓越理财资格路径",
        _enum("existing_premier", "can_qualify_with_1m_trb", "not_applicable"),
        "ask_hsbc_premier_path", "请选择与您相符的汇丰卓越理财情况。"),
    "us_hsbc_account_path": (
        "美国汇丰账户情况",
        _enum("existing_premier_checking", "plan_to_open", "not_applicable"),
        "ask_us_hsbc_account_path", "您目前是否已有美国汇丰卓越理财支票账户？"),
    "uk_hsbc_account_path": (
        "英国汇丰账户情况",
        _enum("student_account", "premier_account", "plan_to_open", "not_applicable"),
        "ask_uk_hsbc_account_path", "您目前是否已有汇丰英国学生或卓越理财账户？"),
    "hk_resident": ("是否为香港居民", _bool, "ask_hk_resident",
                    "您目前是否为香港特别行政区居民？"),
}


BOOLEAN_FIELDS = frozenset(code for code, spec in FIELDS.items() if spec[1] is _bool)

OPTION_LABELS = {
    "role": {"student": "学生本人", "parent": "家长"},
    "destination_country": {"UK": "英国", "US": "美国", "CN-HK": "中国香港"},
    "visa_category": {"student_visa": "学生签证", "visitor_visa": "访问签证", "graduate_visa": "毕业生签证",
                      "dependant_visa": "陪读 / 家属签证"},
    "offer_status": {"none": "尚未收到录取通知", "conditional": "有条件录取", "unconditional": "无条件录取"},
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
        "under_2k": "少于二千港元",
        "2k_5k": "二千至不足五千港元",
        "5k_10k": "五千至不足一万港元",
        "10k_20k": "一万至不足二万港元",
        "20k_plus": "二万港元或以上",
    },
    "mainland_payment_method": {
        "mobile_or_qr": "主要使用手机支付或银联二维码",
        "physical_card": "主要刷实体卡",
        "mixed_or_unsure": "两者差不多，或暂不确定",
    },
    "hsbc_relationship_balance_band": {
        "under_100k": "少于十万港元",
        "100k_999k": "十万至不足一百万港元",
        "1m_plus": "一百万港元或以上",
    },
    "hsbc_premier_path": {
        "existing_premier": "已经是汇丰卓越理财客户，并可满足信用卡关系结余要求",
        "can_qualify_with_1m_trb": "可在汇丰维持一百万港元总关系结余，并愿意先升级卓越理财",
        "not_applicable": "以上均不适用，或不考虑卓越理财",
    },
    "us_hsbc_account_path": {
        "existing_premier_checking": "已有美国汇丰卓越理财支票账户",
        "plan_to_open": "到美国后计划先开卓越理财支票账户，再申请信用卡",
        "not_applicable": "目前没有美国汇丰卓越理财支票账户，想先了解其他选择",
    },
    "uk_hsbc_account_path": {
        "student_account": "已有（或确定会开）汇丰英国学生账户",
        "premier_account": "已有汇丰英国卓越理财或私人银行账户",
        "plan_to_open": "到英国后计划先开学生或卓越理财账户，再申请信用卡",
        "not_applicable": "暂时没有，也不考虑开汇丰英国账户",
    },
    "spending_scenarios": {"online": "网购", "dining": "餐饮", "travel": "旅行",
                           "overseas": "境外消费", "mainland_china": "内地消费",
                            "tuition": "学费", "retail": "日常零售",
                            "groceries": "超市 / 日常采购"},
    "want_credit_card": {
        "yes": "想，请帮我看看",
        "no": "暂时不考虑信用卡",
    },
    "study_card_focus": {
        "lower_fee": "优先控制年费，兼顾日常使用",
        "travel_benefits": "更看重旅行权益，能接受较高年费",
        "compare": "先看两张卡的对比，不急着选",
    },
    "applicant_residence": {"CN": "中国内地", "MO": "澳门", "TW": "台湾", "other": "其他地区"},
    "hk_sponsor": {"institution": "录取院校", "individual": "个人"},
}

# Extra semantic guidance is consumed by the small understanding model only.
# Customer-facing surfaces continue to use the labels/options above; internal
# enum codes never need to appear in copy shown to the user.
UNDERSTANDING_HINTS = {
    "annual_income_hkd": "年薪、年收入、税前年收入；中文“万”按 10000 换算为港币金额",
    "spending_scenarios": (
        "信用卡主要用途；国内消费、回内地刷卡、回国用卡映射为 mainland_china，"
        "网购映射为 online，吃饭映射为 dining，出游/机酒映射为 travel"
    ),
    "mainland_payment_method": (
        "只描述中国内地消费的付款习惯；微信/支付宝/云闪付/二维码属于 mobile_or_qr，"
        "刷实体卡属于 physical_card"
    ),
    "repayment_mode": "每月还清/不欠款属于 full；偶尔分期属于 sometimes_carry；常分期或最低还款属于 usually_carry",
    "reward_preference": (
        "现金回赠/返现属于 cashback；航空里数属于 miles；积分属于 points；"
        "都可以、没有偏好、按价值比较属于 no_preference；这是单选字段"
    ),
    "application_region": "申请信用卡的地区，不等于留学目的地",
    "account_region": "开银行账户的地区，不等于留学目的地",
    "applicant_identity": "实际申请产品的人，不一定是当前提问者",
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
                         "max_selections": getattr(check, "max_items", None),
                         "understanding_hint": UNDERSTANDING_HINTS.get(code)}
    return catalog


FINANCIAL_FIELDS = frozenset({"annual_income_hkd", "annual_income_band_hkd", "asset_band",
                              "hsbc_relationship_balance_band", "hsbc_premier_path",
                              "us_hsbc_account_path", "uk_hsbc_account_path",
                              "monthly_card_spend_band_hkd", "repayment_mode", "amount"})
PERSONAL_FIELDS = frozenset({"applicant_age", "applicant_age_band", "residence_region",
                             "applicant_residence", "hk_resident", "full_time_student",
                             "student_institution_eligible"})
VOLATILE_FIELDS = {"annual_income_hkd": 365, "annual_income_band_hkd": 365, "asset_band": 365,
                   "repayment_mode": 365, "monthly_card_spend_band_hkd": 365,
                   "us_hsbc_account_path": 180, "uk_hsbc_account_path": 180,
                   "mainland_payment_method": 365,
                   "full_time_student": 365, "student_institution_eligible": 365,
                   "premier_relationship": 180, "hsbc_relationship_balance_band": 180,
                   "hsbc_premier_path": 180, "hk_resident": 365, "offer_status": 90}

# Persistence is semantic, not a property of the input widget.  In particular,
# choosing a market/product for one question must not claim that the student
# owns an account there.  These policies are also exposed to the UI through
# field_definitions().
SESSION_ONLY_FIELDS = frozenset({
    "account_region", "account_product", "application_region",
    "card_product_scope", "want_credit_card",
})
RELATIONSHIP_CONTEXT_FIELDS = frozenset({
    "account_applicant_relation", "application_location",
    "identity_document_type", "guardian_present", "existing_hsbc_hk_customer",
})
STUDY_PLAN_PROPOSAL_FIELDS = frozenset({
    "destination_country", "school", "course", "planned_start_date",
    "visa_category",
})
ALWAYS_CONFIRM_FIELDS = frozenset({
    *FINANCIAL_FIELDS,
})


def persistence_policy(code: str) -> dict:
    if code in SESSION_ONLY_FIELDS:
        return {"storage_class": "session_only", "confirmation_mode": "never",
                "scope": "market" if code in {"account_region", "application_region"}
                else "conversation"}
    if code in RELATIONSHIP_CONTEXT_FIELDS:
        return {"storage_class": "relationship", "confirmation_mode": "never",
                "scope": "relationship"}
    if code in STUDY_PLAN_PROPOSAL_FIELDS:
        return {"storage_class": "core_profile", "confirmation_mode": "proposal",
                "scope": "study_plan"}
    if code in ALWAYS_CONFIRM_FIELDS:
        return {"storage_class": "core_profile", "confirmation_mode": "always_confirm",
                "scope": "global"}
    if code in {"spending_scenarios", "study_card_focus", "reward_preference",
                "mainland_payment_method"}:
        return {"storage_class": "preference", "confirmation_mode": "direct_answer",
                "scope": "global"}
    return {"storage_class": "core_profile", "confirmation_mode": "direct_answer",
            "scope": "global"}


def field_definitions():
    """Versioned registry persisted by the service for audit and UI rendering."""
    result = {}
    for code, item in field_catalog().items():
        domains = [name for name, rule in CONDITION_TABLE.items()
                   if code in (*rule.must_have, *rule.nice_to_have)] if "CONDITION_TABLE" in globals() else []
        if code in {"want_credit_card", "application_region", "applicant_age_band",
                    "annual_income_band_hkd", "asset_band",
                    "repayment_mode", "spending_scenarios", "study_card_focus",
                    "reward_preference", "full_time_student",
                    "student_institution_eligible",
                    "premier_relationship", "hsbc_relationship_balance_band", "hsbc_premier_path",
                    "us_hsbc_account_path", "uk_hsbc_account_path",
                    "monthly_card_spend_band_hkd", "mainland_payment_method", "hk_resident"}:
            domains.append("credit_card")
        if code in {"account_applicant_relation", "applicant_identity", "applicant_age",
                    "applicant_age_band", "uk_residency_years",
                    "application_location", "identity_document_type", "has_hk_identity_card",
                    "qualifying_course_offer", "tax_certificate_or_ssn", "guardian_present",
                    "existing_hsbc_hk_customer"}:
            domains.append("bank_account")
        result[code] = {**item, "domains": sorted(set(domains)),
                        **persistence_policy(code),
                        "sensitivity": "financial" if code in FINANCIAL_FIELDS else
                                       "personal" if code in PERSONAL_FIELDS else "standard",
                        "freshness_days": VOLATILE_FIELDS.get(code), "ask_priority": 0,
                        "ask_only_when_needed": True}
    return result


# Exact-value and band variants of the same underlying condition route to the
# same domains; field_definitions only tags the band form for some fields.
_FIELD_DOMAIN_ALIASES = {
    "annual_income_hkd": "annual_income_band_hkd",
    "applicant_age": "applicant_age_band",
}


def domains_for_fields(codes) -> list[str]:
    """Domains whose advice depends on any of the given field codes."""
    definitions = field_definitions()
    domains: set[str] = set()
    for code in codes:
        for lookup in (code, _FIELD_DOMAIN_ALIASES.get(code)):
            if lookup and lookup in definitions:
                domains.update(definitions[lookup].get("domains") or ())
    return sorted(domains)


_DOMAIN_ADVICE_TOPIC = {
    "credit_card": "credit_card",
    "bank_account": "account",
    "hsbc_account": "account",
    "visa": "visa",
    "visa_documents": "visa",
    "fx": "remittance",
    "remittance_tuition": "remittance",
    "remittance_living": "remittance",
    "overseas_spending": "remittance",
}


def advice_topics_for_fields(codes) -> list[str]:
    """Saved advice cards whose inputs include any changed Profile field."""
    return sorted({
        topic for domain in domains_for_fields(codes)
        if (topic := _DOMAIN_ADVICE_TOPIC.get(domain))
    })


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


# Shared fields (e.g. applicant_age) need topic-specific copy so visa asks do not
# sound like bank/card product questions.
TOPIC_QUESTION_OVERRIDES = {
    "visa_documents": {
        "applicant_age": "申请人现在多大？（部分签证材料与是否未满 18 岁有关）",
        "applicant_age_band": "申请人目前是否已满 18 岁？（部分签证材料与此有关）",
    },
    "credit_card": {
        "applicant_age": "申请人现在多大？（年龄会影响可选产品和申请要求）",
        "applicant_age_band": "您目前是否已满 18 岁？（年龄会影响可选信用卡）",
    },
    "bank_account": {
        "applicant_age": "申请人现在多大？（年龄会影响开户要求）",
        "applicant_age_band": "您目前是否已满 18 岁？（年龄会影响开户要求）",
    },
}


def _question(field_code, rule, version, text=None, *, topic=None):
    _, _, code, default = FIELDS[field_code]
    item = field_catalog()[field_code]
    input_type = "multi_select" if item["kind"] == "multi_enum" else \
        "single_select" if item["options"] else item["kind"] if item["kind"] in ("date",) else "text"
    policy = persistence_policy(field_code)
    persist = "session_only" if policy["confirmation_mode"] == "never" else \
        "proposal" if policy["confirmation_mode"] == "proposal" else \
        "save_on_select" if item["options"] or item["kind"] == "date" else "proposal"
    prompt = text or TOPIC_QUESTION_OVERRIDES.get(topic or "", {}).get(field_code) or default
    return FollowUpQuestion(question_code=code, field=field_code, text=prompt,
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
    # expected_profile_version is still accepted for callers; a version bump no longer
    # surfaces a customer-facing notice (re-assess silently on the current profile).

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
        # Conversation state also carries workflow-owned facts (for example
        # ``fx_mode``).  They are useful to their domain workflow but are not
        # profile fields and therefore must not enter profile validation or
        # follow-up question construction.
        if code not in FIELDS:
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

    # 档案已是留学场景时，不必再追问「办哪种签证」。
    if topic == "visa_documents" and "visa_category" not in facts:
        role = facts.get("role").value if "role" in facts else None
        identity = facts.get("applicant_identity").value if "applicant_identity" in facts else None
        school = facts.get("school").value if "school" in facts else None
        offer = facts.get("offer_status").value if "offer_status" in facts else None
        if (role == "student" or identity == "student" or school
                or offer in {"conditional", "unconditional"}):
            facts["visa_category"] = Fact("student_visa", "conversation")
            notices.append(Notice(
                code="visa_category_inferred",
                message="已按留学/在读场景将签证类别视为学生签证；若不是请说明。",
            ))

    conflicted = {code for code, _, _ in conflicts}
    missing = [f for f in rule.must_have if f not in facts and f not in conflicted]
    candidates = [(code, f"invalid.{code}", None) for code, _, _ in invalid]
    candidates += [(code, f"conflict.{code}",
                    f"档案中{label(code)}是「{old}」，这次您提到「{new}」。以哪一个为准？"
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
    questions = [_question(code, rule_id, version, text, topic=topic)
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
