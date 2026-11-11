"""Credit-card recommendation workflow backed by the shared RDS product tables."""

import re
from collections import Counter
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from math import log2

import anyio

from agent2 import clarify
from agent2.intent import (is_card_usage_followup_answer,
                           is_card_usage_profile_statement)
from agent2.models import AnswerBody, FollowUpQuestion, Notice
from agent2.tuition_knowledge import resolve_tuition

from ..composer import is_necessity_question
from ..capabilities import RuleCapabilityPlanner
from ..contracts import DomainResult, DomainTask, OrchestrationInput
from .credit_card_advisor import CreditCardAdvisor
from .credit_card_knowledge import CreditCardKnowledgeService
from .credit_card_engine import SCENARIO_RULES, assess_eligibility, needs_asset_information, recommend
from .credit_card_models import CardCatalogRead, CreditCardRecommendationSet
from .credit_card_repository import (CreditCardRepository, CreditCardRepositoryError,
                                     RdsCreditCardRepository)

SCENARIO_WORDS = {
    "online": ("网购", "线上", "电商", "online"),
    "dining": ("餐饮", "吃饭", "餐厅", "dining"),
    "travel": ("旅行", "旅游", "机票", "酒店", "travel"),
    "overseas": ("境外", "海外", "外币", "overseas"),
    "mainland_china": ("内地", "大陆", "国内消费", "国内刷", "回国用卡", "回国刷卡", "mainland"),
    "tuition": ("学费", "tuition"),
    "retail": ("超市", "购物", "零售", "retail"),
}
REGION_WORDS = {"CN-HK": ("香港", "hong kong", "hk"), "UK": ("英国", "uk"),
                "US": ("美国", "us"), "CN": ("内地", "大陆", "中国")}

CURRENCY_ZH = {"HKD": "港元", "USD": "美元", "GBP": "英镑"}


def _usd_amount_zh(raw: str) -> str:
    """Render a stored USD amount in Chinese reading style (100000 -> 10 万)."""
    digits = str(raw).replace(",", "")
    if not digits.isdigit():
        return str(raw)
    value = int(digits)
    if value >= 10000 and value % 10000 == 0:
        return f"{value // 10000} 万"
    return f"{value:,}"
NETWORK_ZH = {"Visa": "威士卡", "Mastercard": "万事达卡", "UnionPay": "银联卡"}
WAIVERS_ZH = {
    "First year waived": "首年免年费",
    "First two years waived": "首两年免年费",
    "First two years annual fee waiver": "首两年免年费",
    "Perpetual annual fee waiver": "永久免年费",
    "No annual fee": "免年费",
    "Annual fee waived": "年费豁免",
    "First year waived; subsequent years waived with HKD80,000 annual eligible spend":
        "首年免年费；其后每年合资格消费满八万港元可豁免年费",
    "Waived for Private Banking relationship or HSBC US employee status":
        "符合私人银行关系或汇丰美国员工条件可获豁免",
    "Waived for qualifying U.S. HSBC Jade or Private Banking relationship":
        "符合美国汇丰尚玉或私人银行关系条件可获豁免",
}
BENEFITS_ZH = {
    # These are catalogue identifiers, not customer-facing copy.  Keep this
    # translation at the domain boundary so a new or older RDS row cannot
    # expose implementation vocabulary in a recommendation.
    "selected_lifestyle": "指定生活消费类别",
    "lifestyle": "生活消费类别奖励",
    "Enhanced travel protection": "升级旅游保障",
    "Platinum lifestyle offers": "白金卡生活礼遇",
    "Retail partner discounts": "合作零售商优惠",
    "Mobile wallet support": "支持手机钱包支付",
    "Visa concierge": "威士礼宾服务",
    "Airport lounge or dining access": "机场贵宾室或餐饮礼遇",
    "Global concierge": "全球礼宾服务",
    "Airport lounge access": "机场贵宾室礼遇",
    "HKD and CNY settlement": "港元及人民币双币结算",
    "Mainland China merchant offers": "内地商户优惠",
    "Instant digital card access": "批核后可即时使用数码卡",
    "Online merchant offers": "网上商户优惠",
    "Priority Pass lounge access": "机场贵宾室礼遇",
    "Reward points with airline and hotel partners": "积分可用于航空及酒店合作伙伴",
    "Rewards points with airline and hotel transfer partners": "积分可转至航空及酒店合作伙伴",
    "Premier worldwide travel insurance (account benefit)": "卓越理财账户附带全球旅游保险",
    "Enhanced travel lounge / fast-track privileges": "升级机场贵宾室及快速通道礼遇",
    "Faster reward points earn": "较高积分赚取比率",
    "Lost/stolen card support and emergency cash advance": "遗失或被盗卡支援及紧急现金预支",
    "UnionPay QR payments": "支持银联二维码支付",
    "Global merchant offers": "全球商户优惠",
    "Student spending tools": "学生理财工具",
    "Signature dining privileges": "御玺餐饮礼遇",
    "Travel protection": "旅游保障",
    "HKD and RMB dual currency": "港元及人民币双币结算",
    "Mainland China and Macau offers": "内地及澳门商户优惠",
}
BENEFITS_ZH.update({
    "Airport screening statement credit (up to $85 / 4.5 years)":
        "机场安检计划申请费签账回赠：每四年半最高八十五美元",
    "Airport screening statement credit (up to $120 / 4.5 years)":
        "机场安检计划申请费签账回赠：每四年半最高一百二十美元",
    "Streaming statement credit (up to $60 / year)": "串流服务签账回赠：每年最高六十美元",
    "Instacart+ benefits (up to $140 / year)": "指定送货平台礼遇：每年最高一百四十美元",
    "Cell phone protection (up to $600 / claim)": "手机保障：每次索赔最高六百美元",
    "HSBC Travel credit ($400 / year)": "汇丰旅游平台消费回赠：每年四百美元",
    "Taxi and rideshare credits (up to $120 / year)":
        "出租车及网约车签账回赠：每年最高一百二十美元",
})
PROMOS_ZH = {
    "Extra 1.2% RewardCash automatically on all Rewards of Your Choice categories (promotional, until 31 December 2026)":
        "「最红自主奖赏」所有类别额外百分之一点二奖赏钱，推广期至二〇二六年十二月三十一日",
    "Extra 2.0% RewardCash for QR/mobile payments in Mainland China and Macau in RMB/MOP (promotional; first HKD80,000 eligible spend per promotional period, per HKID, until 31 December 2026)":
        "内地及澳门人民币或澳门币二维码及手机支付额外百分之二奖赏钱；每名持卡人在推广期首八万港元合资格消费适用，推广期至二〇二六年十二月三十一日",
    "Extra 2.0% RewardCash for QR/mobile payments in Mainland China and Macau (promotional, per HKID, until 31 December 2026)":
        "内地及澳门二维码及手机支付额外百分之二奖赏钱；推广期至二〇二六年十二月三十一日",
}
RULES_ZH = {
    "Applicants must be at least 18.": "申请人须年满十八岁",
    "Applicants must be over 18.": "申请人须年满十八岁",
    "Must be a UK resident.": "申请人须为英国居民",
    "Must hold an HSBC UK Premier Account (or Private Banking).":
        "申请人须持有汇丰英国卓越理财或私人银行账户",
    "Must hold an HSBC UK Premier Account.": "申请人须持有汇丰英国卓越理财账户",
    "Must hold an HSBC UK Student Bank Account.": "申请人须持有汇丰英国学生账户",
    "Requires an existing U.S. HSBC Premier checking account.":
        "申请人须持有美国汇丰卓越理财支票账户",
    "仅供HSBC Premier客户申请。": "仅供汇丰卓越理财客户申请",
    "可先以HKD1,000,000总关系结余申请或升级HSBC Premier，再申请信用卡。":
        "可先以一百万港元总关系结余申请或升级汇丰卓越理财，再申请信用卡",
    "基本卡申请人须为香港特别行政区居民。": "基本卡申请人须为香港特别行政区居民",
    "基本卡申请人须年满18岁。": "基本卡申请人须年满十八岁",
    "当前产品页公布最低年收入为HKD120,000。": "官网公布最低年收入为十二万港元",
    "当前产品页公布最低年收入为HKD240,000。": "官网公布最低年收入为二十四万港元",
    "当前产品页公布最低年收入为HKD60,000。": "官网公布最低年收入为六万港元",
    "没有收入证明的学生等申请人可提交最近3个月的银行资产证明，由HSBC个案审批。":
        "没有收入证明的学生等申请人可提交最近三个月的银行资产证明，由汇丰个案审批",
    "申请人须为全日制大专或大学生。": "申请人须为全日制大专或大学生",
    "申请人须就读HSBC列出的香港指定院校。": "申请人须就读汇丰列出的香港指定院校",
    "申请后两个月内全面理财总值须至少HKD100,000（不含借贷）。":
        "申请后两个月内全面理财总值须至少十万港元，不含借贷",
    "电话申请声明公布资产HKD400,000或以上的替代路径。":
        "电话申请另有资产四十万港元或以上的替代路径",
}


def _waiver_text(value):
    return WAIVERS_ZH.get(value, "年费豁免条件请见官网")


def _benefit_text(value):
    return BENEFITS_ZH.get(value, "其他权益请见官网")


def _category_text(value):
    """Return public copy for a reward category stored in the card catalogue."""
    labels = {
        "online": "网购", "dining": "餐饮", "travel": "旅行",
        "travel_and_transport": "旅行与交通", "overseas": "境外消费",
        "mainland_china": "内地消费", "mainland_china_and_macau": "内地及澳门消费",
        "retail": "零售消费", "groceries": "日常采购", "general": "日常消费",
        "selected_lifestyle": "指定生活消费类别",
        "selected_retail": "指定零售类别", "dining_and_lifestyle": "餐饮及生活消费",
        "gas_and_groceries": "加油及日常采购",
    }
    # An unknown snake_case value is internal data, not suitable fallback copy.
    return labels.get(value, "指定消费类别" if re.fullmatch(r"[a-z]+(?:_[a-z]+)+", value or "")
                      else "日常消费")


def _money(amount, currency):
    return f"{amount:g} {CURRENCY_ZH.get(currency, '元')}"


def _public_reason(reason):
    prefix, sep, detail = reason.partition("：")
    if not sep:
        return RULES_ZH.get(reason, "具体申请条件请见官网" if re.search(r"[A-Za-z]", reason)
                            else reason)
    translated = RULES_ZH.get(detail, "具体申请条件请见官网"
                              if re.search(r"[A-Za-z]", detail) else detail)
    return f"{prefix}：{translated}"


def extract_credit_card_facts(text: str):
    lower = text.casefold()
    facts, basis = {}, {}
    if (re.search(r"已开好|已开立|已经开好|已经开了|刚开好", text)
            and re.search(r"(?:美国汇丰.{0,8}卓越理财支票账户|(?:美国汇丰|hsbc).{0,12}premier\s*checking)",
                          text, re.I)):
        facts["us_hsbc_account_path"] = "existing_premier_checking"
        basis["us_hsbc_account_path"] = "客户确认已开立美国汇丰 Premier Checking"
    card_positions = [match.start() for match in re.finditer(
        r"信用卡|credit\s*card", lower, re.I)]
    region_matches = []
    for region, words in REGION_WORDS.items():
        for word in words:
            for match in re.finditer(re.escape(word.casefold()), lower):
                position = match.start()
                for card_position in card_positions:
                    distance = abs(position - card_position)
                    if distance <= 8:
                        # Prefer the nearest region attached to "credit card".
                        # A banking relationship mentioned later is not the
                        # application region (e.g. UK card, HK Premier).
                        region_matches.append((distance, position > card_position,
                                               -len(word), region, word))
    if region_matches:
        _, _, _, region, word = min(region_matches)
        facts["application_region"], basis["application_region"] = region, word
    income = re.search(r"(?:年收入|年薪)\D{0,8}(\d+(?:\.\d+)?)\s*(万|[kK])?", text)
    if income:
        value = float(income.group(1))
        value *= 10000 if income.group(2) == "万" else 1000 if income.group(2) else 1
        facts["annual_income_hkd"], basis["annual_income_hkd"] = int(value), income.group(0)
    if re.search(r"每月.{0,4}全额还|全额还款|pay.{0,4}full", text, re.I):
        facts["repayment_mode"], basis["repayment_mode"] = "full", "全额还款"
    elif re.search(r"经常分期|通常分期|最低还款|保留余额|欠款", text):
        facts["repayment_mode"], basis["repayment_mode"] = "usually_carry", "可能长期保留余额"
    elif re.search(r"偶尔分期|有时分期", text):
        facts["repayment_mode"], basis["repayment_mode"] = "sometimes_carry", "偶尔分期"
    scenarios = [code for code, words in SCENARIO_WORDS.items()
                 if any(word.casefold() in lower for word in words)]
    if scenarios:
        facts["spending_scenarios"] = scenarios
        basis["spending_scenarios"] = "、".join(
            clarify.OPTION_LABELS["spending_scenarios"].get(code, code) for code in scenarios)
    mainland_context = r"(?:内地|大陆|国内|回国)"
    mobile_payment = r"(?:微信|支付宝|云闪付|二维码|扫码|手机支付)"
    physical_payment = r"(?:实体卡|实卡|插卡|拍卡|刷卡)"
    if (re.search(fr"{mainland_context}.{{0,12}}{mobile_payment}", text)
            or re.search(fr"{mobile_payment}.{{0,12}}{mainland_context}", text)):
        facts["spending_scenarios"] = list(dict.fromkeys([
            *(facts.get("spending_scenarios") or []), "mainland_china"]))
        basis["spending_scenarios"] = "内地消费"
        facts["mainland_payment_method"] = "mobile_or_qr"
        basis["mainland_payment_method"] = next(
            match.group(0) for pattern in (
                fr"{mainland_context}.{{0,12}}{mobile_payment}",
                fr"{mobile_payment}.{{0,12}}{mainland_context}",
            ) if (match := re.search(pattern, text)))
    elif (re.search(fr"{mainland_context}.{{0,12}}{physical_payment}", text)
          or re.search(fr"{physical_payment}.{{0,12}}{mainland_context}", text)):
        facts["spending_scenarios"] = list(dict.fromkeys([
            *(facts.get("spending_scenarios") or []), "mainland_china"]))
        basis["spending_scenarios"] = "内地消费"
        facts["mainland_payment_method"] = "physical_card"
        basis["mainland_payment_method"] = "内地刷实体卡"
    if re.search(r"全日制.{0,4}学生|学生卡", text):
        facts["full_time_student"], basis["full_time_student"] = True, "全日制学生"
    premier_denied = re.search(
        r"(?:不是|并非|没有|非|不属于)\s*(?:HSBC\s*)?(?:卓越理财|Premier)"
        r"|\b(?:not|no)\s+(?:an?\s+)?(?:HSBC\s+)?Premier\b", text, re.I)
    if re.search(r"卓越理财|premier", text, re.I) and not premier_denied:
        facts["premier_relationship"], basis["premier_relationship"] = True, "HSBC Premier"
    for value, words in {"cashback": ("返现", "现金回赠"), "points": ("积分",),
                         "miles": ("里程", "航空里数")}.items():
        if any(word in text for word in words):
            facts["reward_preference"], basis["reward_preference"] = value, next(w for w in words if w in text)
            break
    # 必要性问句里的「办信用卡吗」不算已确认想办；明确拒绝/主动要推荐才写入。
    if re.search(
            r"(?:先不|暂时不|不想|不必|不用|不需要).{0,6}(?:办|申请|了解).{0,6}信用卡"
            r"|信用卡.{0,6}(?:先不|不用|不要|不必)", text):
        facts["want_credit_card"], basis["want_credit_card"] = "no", "先不办信用卡"
    elif not is_necessity_question(text) and re.search(
            r"(?:想|要|准备|打算)(?:办|申请|了解|比较).{0,8}信用卡"
            r"|(?:推荐|比较|挑选|了解).{0,8}(?:信用卡|汇丰卡)"
            r"|办一张.{0,4}卡", text):
        facts["want_credit_card"], basis["want_credit_card"] = "yes", "想办信用卡"
    elif is_necessity_question(text) and re.search(
            r"(?:顺便|直接|如果需要.?帮我).{0,6}推荐|帮我比较.{0,4}卡", text):
        facts["want_credit_card"], basis["want_credit_card"] = "yes", "想比较信用卡"
    return facts, basis


# Situation-referencing honorifics swap for the profile name; conversational
# politeness ("您提到", "您想", "您可以", "如果您愿意") keeps addressing the
# person typing. Longest patterns first so 您会更喜欢 wins over 您的.
_ADDRESS_PATTERNS = (
    "您会更喜欢", "您比较喜欢", "您愿意考虑", "您在汇丰的", "您通常",
    "您的", "您是否", "您每月", "您每年", "您平时", "您最常", "您属于",
)


_NO_SPACE_BEFORE = set("，。？！；：、（([【")


def _addressed(text: str, profile_name: str) -> str:
    """Rewrite situation-specific 您 into the profile's display name.

    The profile is often prepared by a parent for a child (role=parent), so
    questions about age, income or habits should address the applicant on
    file, not the person typing. Polite/interactive copy keeps 您. Empty or
    missing names keep the original text.
    """
    name = (profile_name or "").strip()[:30]
    if not name:
        return text
    pattern = "|".join(_ADDRESS_PATTERNS)

    def substitute(match):
        # Latin names next to Chinese read better with separating spaces,
        # except right after punctuation.
        prefix = "" if match.start() == 0 or text[match.start() - 1] in _NO_SPACE_BEFORE else " "
        return f"{prefix}{name} {match.group()[1:]}"

    return re.sub(pattern, substitute, text)


def _question(field, text, version, *, options, input_type="single_select", max_selections=None):
    policy = clarify.persistence_policy(field)
    persist_mode = "session_only" if policy["confirmation_mode"] == "never" else \
        "proposal" if policy["confirmation_mode"] == "proposal" else "save_on_select"
    return FollowUpQuestion(question_code=f"ask_{field}", field=field, text=text,
                            rule="credit_card_recommendation_v1", profile_version=version,
                            input_type=input_type, options=options, max_selections=max_selections,
                            persist_mode=persist_mode)


def _income_options(products):
    thresholds = sorted({int(rule.value_numeric) for product in products
                         for rule in product.eligibility_rules
                         if rule.required and rule.rule_type == "minimum_income"
                         and rule.currency == "HKD" and rule.value_numeric is not None})
    if not thresholds:
        return []
    options = [{"value": f"lt_{thresholds[0]}", "label": f"少于 {thresholds[0]:,} 港元"}]
    for lower, upper in zip(thresholds, thresholds[1:]):
        options.append({"value": f"range_{lower}_{upper - 1}",
                        "label": f"{lower:,} 至 {upper - 1:,} 港元"})
    options.append({"value": f"gte_{thresholds[-1]}", "label": f"{thresholds[-1]:,} 港元或以上"})
    return options


QUESTION_OPTIONS = {
    "want_credit_card": [
        {"value": "yes", "label": "想，帮我比较一下"},
        {"value": "no", "label": "这轮先不办，只看开户或其他事项"},
    ],
    "application_region": [{"value": "UK", "label": "英国"},
                           {"value": "US", "label": "美国"},
                           {"value": "CN-HK", "label": "中国香港"}],
    "applicant_age_band": [{"value": "under_18", "label": "18 岁以下"},
                           {"value": "18_or_over", "label": "18 岁或以上"}],
    "hk_resident": [{"value": True, "label": "是，我是香港居民"},
                    {"value": False, "label": "否，我不是香港居民"}],
    "us_hsbc_account_path": [
        {"value": "existing_premier_checking",
         "label": "已有美国汇丰卓越理财支票账户"},
        {"value": "plan_to_open",
         "label": "到美国后计划先开卓越理财支票账户，再申请信用卡"},
        {"value": "need_opening_guidance",
         "label": "还没有美国汇丰卓越理财支票账户，想先了解开户再办信用卡"},
    ],
    "uk_hsbc_account_path": [
        {"value": "student_account",
         "label": "已有英国汇丰学生账户"},
        {"value": "premier_account",
         "label": "已有英国汇丰卓越理财或私人银行账户"},
        {"value": "plan_to_open",
         "label": "还没有，计划先开英国学生或卓越理财账户"},
    ],
    "hsbc_relationship_balance_band": [
        {"value": "under_100k", "label": "少于十万港元"},
        {"value": "100k_999k", "label": "十万至不足一百万港元"},
        {"value": "1m_plus", "label": "一百万港元或以上"},
    ],
    "hsbc_premier_path": [
        {"value": "existing_premier",
         "label": "已经是汇丰卓越理财客户，并可满足信用卡关系结余要求"},
        {"value": "can_qualify_with_1m_trb",
         "label": "可在汇丰维持一百万港元总关系结余，并愿意先升级卓越理财"},
        {"value": "not_applicable", "label": "以上均不适用，或不考虑卓越理财"},
    ],
    "asset_band": [
        {"value": "none", "label": "目前无法提供银行资产证明"},
        {"value": "under_100k", "label": "可证明资产少于十万港元"},
        {"value": "100k_500k", "label": "可证明资产十万至五十万港元"},
        {"value": "500k_plus", "label": "可证明资产五十万港元或以上"},
    ],
    "full_time_student": [
        {"value": True, "label": "是，我是全日制学生"},
        {"value": False, "label": "否，我不是全日制学生"},
    ],
    "student_institution_eligible": [
        {"value": True, "label": "是，我就读汇丰指定名单内的香港院校课程"},
        {"value": False, "label": "否，或暂不确定"},
    ],
    "repayment_mode": [{"value": "full", "label": "每月全额还款"},
                       {"value": "sometimes_carry", "label": "偶尔分期或保留余额"},
                       {"value": "usually_carry", "label": "通常会保留余额"}],
    "spending_scenarios": [{"value": "online", "label": "网购"},
                           {"value": "dining", "label": "餐饮"},
                           {"value": "travel", "label": "旅行"},
                           {"value": "overseas", "label": "境外消费"},
                           {"value": "mainland_china", "label": "内地消费"},
                           {"value": "tuition", "label": "学费"},
                           {"value": "retail", "label": "日常零售"}],
    "study_card_focus": [
        {"value": "lower_fee", "label": "优先控制年费，兼顾日常使用"},
        {"value": "travel_benefits", "label": "更看重旅行权益，能接受较高年费"},
        {"value": "compare", "label": "先看两张卡的对比，不急着选"},
    ],
    "monthly_card_spend_band_hkd": [
        {"value": "under_2k", "label": "少于二千港元"},
        {"value": "2k_5k", "label": "二千至不足五千港元"},
        {"value": "5k_10k", "label": "五千至不足一万港元"},
        {"value": "10k_20k", "label": "一万至不足二万港元"},
        {"value": "20k_plus", "label": "二万港元或以上"},
    ],
    "mainland_payment_method": [
        {"value": "mobile_or_qr", "label": "主要使用手机支付或银联二维码"},
        {"value": "physical_card", "label": "主要刷实体卡"},
        {"value": "mixed_or_unsure", "label": "两者差不多，或暂不确定"},
    ],
    "reward_preference": [
        {"value": "cashback", "label": "现金回赠"},
        {"value": "points", "label": "积分"},
        {"value": "miles", "label": "航空里程"},
        {"value": "no_preference", "label": "没有偏好，按预计价值比较"},
    ],
}


# Study destination → local HSBC shelf first, plus Hong Kong as a separate backup.
# Groups stay ranked apart; HKD net value is never compared with USD or GBP.
STUDY_MARKETS = {"UK": ("UK", "CN-HK"), "US": ("US", "CN-HK")}
US_CARDS_URL = "https://www.us.hsbc.com/credit-cards/"
US_PREMIER_CHECKING_URL = "https://www.us.hsbc.com/checking-accounts/products/premier/"
# Profile destination_country can stand in for application_region when it is a
# known card market (including Hong Kong — do not re-ask where they study).
DESTINATION_AS_REGION = frozenset({"UK", "US", "CN-HK"})

GROUP_TITLES = {
    "UK": "优先：汇丰英国发行的卡",
    "US": "优先：汇丰美国发行的卡",
    "CN-HK": "汇丰香港发行的卡",
}
HK_BACKUP_TITLE = "备用：汇丰香港发行的卡（获批可能较难）"
# Official eligibility, stated alongside cards that remain candidates.
PREREQUISITES = {
    "US": (
        "汇丰美国两款信用卡均要求已有美国汇丰卓越理财支票账户"
        "（内地或香港的卓越理财账户不能替代），申请仍须通过银行审批。"
    ),
    "UK": (
        "到英国读书：学生卡一般须年满 18 岁、为英国居民，并已持有汇丰英国学生账户；"
        "卓越理财卡须已有汇丰英国卓越理财或私人银行关系。"
        "从内地出发、账户尚未开立时，通常需到英国后再申请。下面仍列出产品供了解。"
    ),
    "CN-HK": (
        "香港卡可在留学地使用；外币手续费以该卡官网为准。"
    ),
}

_STATUS_ORDER = {"eligible": 0, "needs_bank_assessment": 1, "unknown": 2, "ineligible": 3}

_CARD_NAME_STOP_WORDS = {
    "hsbc", "credit", "card", "visa", "mastercard", "world", "hong", "kong",
    "dual", "currency",
}


def _named_card_products(text, products):
    """Resolve explicit catalog product names without requiring database codes."""
    folded = text.casefold()
    matches = []
    for product in products:
        name = product.product_name.casefold()
        aliases = [name]
        aliases += [token for token in re.findall(r"[a-z0-9]+", name)
                    if len(token) >= 3 and token not in _CARD_NAME_STOP_WORDS]
        hit = max((alias for alias in aliases
                   if (alias in folded if " " in alias else
                       re.search(rf"(?<![a-z0-9]){re.escape(alias)}(?![a-z0-9])", folded))),
                  key=len, default=None)
        if hit:
            matches.append((folded.find(hit), product))
    return [product for _, product in sorted(matches, key=lambda item: item[0])]


def _looks_like_card_product_question(text):
    return bool(re.search(
        r"怎么样|好不好|值得|适合|介绍|讲讲|评价|优缺点|"
        r"年费|费用|权益|回赠|返现|积分|里程|申请条件|能不能申请|区别|对比|哪个好",
        text, re.I))


def _refers_to_card(text):
    return bool(re.search(r"这个卡|这张(?:卡)?|那张(?:卡)?|刚才那张|刚推荐的|刚才推荐的|另一张|备选卡|它", text))


def _requested_card_scope(text):
    """Turn-scoped shelf limits. Never infer these from the student's profile."""
    networks = [network for network, pattern in (
        ("Visa", r"\bvisa\b|威士"),
        ("Mastercard", r"\bmastercard\b|万事达"),
        ("UnionPay", r"\bunionpay\b|银联"),
    ) if re.search(pattern, text, re.I)]
    return {"networks": networks, "no_annual_fee": bool(re.search(r"免年费|零年费|无年费", text))}


def _in_card_scope(product, scope):
    if scope.get("networks") and product.card_network not in scope["networks"]:
        return False
    if scope.get("no_annual_fee"):
        annual = next((fee for fee in product.fees if fee.fee_type == "annual_fee"), None)
        if annual is None or annual.amount != 0:
            return False
    return True


class CreditCardWorkflow:
    def __init__(self, repository: CreditCardRepository | None = None, clock=None, advisor=None,
                 knowledge=None):
        self.repository = repository or RdsCreditCardRepository()
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.advisor = advisor or CreditCardAdvisor(clock=self.clock)
        self.knowledge = knowledge or CreditCardKnowledgeService(clock=self.clock)

    async def run(self, task: DomainTask, request: OrchestrationInput) -> DomainResult:
        if request.input_mode == "free_text":
            rule_facts, rule_basis = extract_credit_card_facts(request.text)
        else:
            rule_facts, rule_basis = {}, {}
        # The rule extractor binds an explicit region to the phrase "信用卡".
        # A model may recover fields the rules missed, but it must never turn
        # “香港信用卡” into a US card merely because the Profile says US study.
        extracted = {**request.understood_facts, **rule_facts}
        basis = {**request.understood_basis, **rule_basis}
        facts = {**request.profile_values, **task.facts, **extracted}
        destination = facts.get("destination_country")
        if facts.get("application_region") is None and destination in DESTINATION_AS_REGION:
            # Rendering default for this turn only.  It must stay out of the
            # observed facts, otherwise the study destination leaks into a
            # persisted card-application region and the two fields couple.
            facts["application_region"] = destination
        self._derive_hk_student_facts(facts)

        if request.input_mode == "free_text":
            prerequisite = re.search(r"美国|Premier Checking", request.text, re.I) and re.search(
                r"怎么申请|如何申请|没有.{0,24}(?:账户|Checking)|替代.{0,24}(?:账户|Checking)|先开户|先开.*账户", request.text, re.I)
            if prerequisite:
                return DomainResult(
                    task_id=task.task_id, domain="credit_card", status="answered",
                    answer=AnswerBody(
                        summary=PREREQUISITES["US"],
                        steps=["先按美国汇丰官网核对并开立 Premier Checking，之后另行申请信用卡并接受银行审批。"]),
                    evidence_refs=[US_CARDS_URL, US_PREMIER_CHECKING_URL],
                    payload={"capability": "general_knowledge"}, projection="chat_only",
                    observed_facts=extracted, observed_basis=basis)
            if re.search(r"信用卡.*(?:必需|必要)|(?:必需|必要).*信用卡", request.text):
                return DomainResult(
                    task_id=task.task_id, domain="credit_card", status="answered",
                    answer=AnswerBody(summary="信用卡不是硬性必需，可按订票、酒店和日常付款需要决定。",
                                      steps=["先确认现有借记卡和付款方式是否够用；需要选卡时再比较费用、权益和申请条件。"]),
                    payload={"capability": "general_knowledge"}, projection="chat_only")

        if re.search(r"(?:未满|不满)\s*18\s*岁|\bunder\s*18\b", request.text, re.I):
            facts["applicant_age_band"] = "under_18"
            if re.search(r"申请|能办|可以|能不能", request.text):
                return DomainResult(
                    task_id=task.task_id, domain="credit_card", status="answered",
                    answer=AnswerBody(summary="未满18岁尚未达到所覆盖信用卡基本卡的申请年龄要求。",
                                      steps=["如考虑附属卡，需要另外核对具体产品的持卡年龄及监护人要求。"]),
                    payload={"capability": "general_knowledge"}, projection="chat_only")

        # Product Q&A is a chat capability, not another recommendation run.
        # It must answer “Red 卡怎么样 / 年费多少 / 和 Pulse 有什么区别”
        # without collecting every preference used by the ranking engine.
        explicit_profile_statement = is_card_usage_profile_statement(request.text)
        scenario_followup_answer = is_card_usage_followup_answer(
            request.text, request.asked_fields)
        profile_statement_or_answer = explicit_profile_statement or scenario_followup_answer
        actions = [*task.actions, *RuleCapabilityPlanner()._enrich_task(task, request.text).actions]
        # "哪张卡比较合适" asks us to choose; "Red 和 Pulse 比较" asks
        # us to compare named products. The word 比较 alone is not decisive.
        explicit_comparison = any(action.operation == "compare" for action in actions) and (
            not re.search(r"比较(?:适合|合适|好|划算)", request.text)
            or len({mention.text.casefold() for action in actions
                    for mention in action.entity_mentions}) >= 2)
        recommendation_requested = bool(re.search(r"(?:想要|想办|推荐).{0,10}免年费.{0,6}信用卡", request.text)) or not request.text.strip() or (
            not explicit_comparison and any(
                action.operation == "recommend" and action.evidence for action in actions))
        capability_qa = not recommendation_requested and any(
            action.operation in {"explain", "compare", "assess", "how_to", "troubleshoot"}
            for action in actions
        )
        if request.input_mode == "free_text" and not profile_statement_or_answer and (capability_qa or
                (not recommendation_requested and _looks_like_card_product_question(request.text))):
            overview = await self._product_question(task, request, facts, extracted, basis)
            if overview is not None:
                return overview
            # Unnamed questions may be servicing questions; give knowledge
            # retrieval a chance before asking for comparison product names.
        # General servicing knowledge is a credit-card-domain capability, not
        # a new router branch.  It is used only when no specific product answer
        # was available above, and it never starts a recommendation interview.
        profile_update_turn = bool(
            (request.domain_state.get("credit_card", {}).get("cards")
             or profile_statement_or_answer)
            and set(extracted) & {"spending_scenarios", "mainland_payment_method",
                                  "repayment_mode", "reward_preference", "annual_income_hkd"})
        if request.input_mode == "free_text" and request.text.strip() and recommendation_requested:
            scope = _requested_card_scope(request.text)
            if extracted.get("application_region"):
                scope["market"] = extracted["application_region"]
            # A fresh request replaces the prior query scope, including with an
            # empty scope when the user asks to compare the full shelf.
            facts["card_query_scope"] = scope
            extracted["card_query_scope"] = scope
            basis["card_query_scope"] = "本轮指定的选卡范围"
        general_knowledge = (await self.knowledge.answer(task, request, facts, extracted, basis)
                             if (request.input_mode == "free_text" and not recommendation_requested
                                 and not profile_statement_or_answer) else None)
        if general_knowledge is not None:
            return general_knowledge

        # 「要不要办信用卡」：先确认意愿，再进入普通选卡流程。
        if facts.get("want_credit_card") is None and is_necessity_question(request.text):
            return DomainResult(
                task_id=task.task_id, domain="credit_card", status="needs_input",
                answer=AnswerBody(
                    # Polite conversational copy addresses the person typing: keep 您.
                    summary="信用卡不是硬性必需，您可以按自己的需要决定。如果您愿意，我可以陪您看看有哪些合适的选择。",
                    steps=[
                        "若选择了解，我会按留学地优先推荐当地汇丰卡，香港卡仅作备用。",
                        "若这次先不办也没关系；之后直接说「推荐信用卡」即可。",
                    ],
                ),
                missing_fields=["want_credit_card"],
                follow_up_questions=[_question(
                    "want_credit_card",
                    "信用卡不是留学的硬性要求。您想先了解一下适合自己的选择吗？",
                    request.profile_version,
                    options=QUESTION_OPTIONS["want_credit_card"],
                )],
                observed_facts=extracted, observed_basis=basis,
            )
        if facts.get("want_credit_card") == "no":
            return DomainResult(
                task_id=task.task_id, domain="credit_card", status="answered",
                answer=AnswerBody(
                    summary="好的，这次先不比较信用卡。之后想办时，直接说「推荐信用卡」就可以。",
                    steps=[],
                ),
                payload={"deferred": True, "groups": [], "market": facts.get("application_region")},
                observed_facts=extracted, observed_basis=basis,
            )

        if (request.input_mode == "free_text" and not recommendation_requested
                and not profile_update_turn
                and re.search(r"[？?]|怎么|如何|为什么|能不能|可以吗|是什么|多少钱|多久|是否", request.text)):
            return DomainResult(
                task_id=task.task_id, domain="credit_card", status="needs_verification",
                answer=AnswerBody(
                    summary="这个信用卡问题目前没有足够的已核验资料让我给出准确结论。",
                    steps=["请告诉我具体卡种和发卡地区，我会按该卡的最新条款继续核对。"]),
                payload={"capability": "general_knowledge", "topic": "unverified_question"},
                projection="chat_only", observed_facts=extracted, observed_basis=basis)

        if (request.input_mode == "free_text" and not recommendation_requested
                and not profile_update_turn
                and not request.session_pending_questions):
            return DomainResult(
                task_id=task.task_id, domain="credit_card", status="answered",
                answer=AnswerBody(summary="您想先了解信用卡的哪一方面？我可以比较指定卡片、说明费用和权益，或在您需要时按条件推荐。"),
                payload={"capability": "general_knowledge"}, projection="chat_only",
                observed_facts=extracted, observed_basis=basis)

        if facts.get("application_region") is None and not request.refresh_mode:
            missing = ["application_region"]
            if facts.get("applicant_age") is None and facts.get("applicant_age_band") is None:
                missing.append("applicant_age_band")
            prompts = {
                "application_region": (
                    "为了先看对申请地区，方便告诉我您准备去哪里留学吗？"
                    "如果是英国或美国，我会先看当地卡，再把香港卡作为备选。"
                ),
                "applicant_age_band": "方便了解您是否已满 18 岁吗？这会影响可申请的卡种。",
            }
            return self._needs_input(task, request, extracted, basis, missing, prompts, QUESTION_OPTIONS)

        if facts["application_region"] in STUDY_MARKETS:
            return await self._run_study(task, request, facts, extracted, basis)

        catalogs = None
        adaptive_hk = facts["application_region"] in {"CN-HK", "HK"}
        if adaptive_hk:
            try:
                catalogs = await self._catalogs(facts["application_region"], facts.get("card_query_scope"))
            except CreditCardRepositoryError:
                return self._repository_unavailable(task, extracted, basis)
            catalog = self._combined(catalogs)
            if not catalog.products:
                return self._empty_catalog(task, catalog, extracted, basis, facts.get("card_query_scope"))
            scope = dict(facts.get("card_query_scope") or {})
            if request.input_mode == "structured_selection" and request.asked_fields:
                scope["questions_answered"] = int(scope.get("questions_answered", 0)) + 1
            facts["card_query_scope"] = scope
            extracted["card_query_scope"] = scope
            question = (None if request.refresh_mode else
                        self._hk_best_question(catalog.products, facts, scope))
            if question:
                field, prompt, options = question
                scope["asked_fields"] = list(dict.fromkeys([
                    *(scope.get("asked_fields") or []), field]))
                facts["card_query_scope"] = scope
                extracted["card_query_scope"] = scope
                return self._needs_input(task, request, extracted, basis, [field],
                                         {field: prompt}, {field: options})

        if (not request.refresh_mode and not adaptive_hk and facts.get("annual_income_hkd") is None
                and facts.get("annual_income_band_hkd") is None):
            try:
                catalogs = await self._catalogs(facts["application_region"], facts.get("card_query_scope"))
            except CreditCardRepositoryError:
                return self._repository_unavailable(task, extracted, basis)
            catalog = self._combined(catalogs)
            if not catalog.products:
                return self._empty_catalog(task, catalog, extracted, basis, facts.get("card_query_scope"))
            missing = []
            prompts, options = {}, dict(QUESTION_OPTIONS)
            if facts.get("applicant_age") is None and facts.get("applicant_age_band") is None:
                missing.append("applicant_age_band")
                prompts["applicant_age_band"] = "方便了解您是否已满 18 岁吗？这会影响可申请的卡种。"
            missing.append("annual_income_band_hkd")
            prompts["annual_income_band_hkd"] = ("为了避开可能不符合申请条件的卡，方便了解您的税前年收入大致在哪个区间吗？"
                                                  "选一个范围就好，不需要提供准确金额。")
            options["annual_income_band_hkd"] = _income_options(catalog.products)
            return self._needs_input(
                task, request, extracted, basis, missing, prompts, options)

        if (not request.refresh_mode and not adaptive_hk and facts.get("applicant_age") is None
                and facts.get("applicant_age_band") is None):
            return self._needs_input(task, request, extracted, basis, ["applicant_age_band"],
                                     {"applicant_age_band": "方便了解您是否已满 18 岁吗？这会影响可申请的卡种。"}, QUESTION_OPTIONS)

        if catalogs is None:
            try:
                catalogs = await self._catalogs(facts["application_region"], facts.get("card_query_scope"))
            except CreditCardRepositoryError:
                return self._repository_unavailable(task, extracted, basis)
            catalog = self._combined(catalogs)
            if not catalog.products:
                return self._empty_catalog(task, catalog, extracted, basis, facts.get("card_query_scope"))

        gate_products = catalog.products
        requires_residency = any(
            rule.required and rule.rule_type == "residency"
            for product in gate_products for rule in product.eligibility_rules
        )
        if not request.refresh_mode and not adaptive_hk and requires_residency and facts.get("hk_resident") is None:
            return self._needs_input(
                task, request, extracted, basis, ["hk_resident"],
                {"hk_resident": (
                    "为免推荐到申请机会较低的卡，方便了解您是否为香港居民吗？"
                    "非香港居民也可以了解产品，但申请仍需银行个案审批。"
                )},
                QUESTION_OPTIONS,
            )

        gate_result = self._all_ineligible_result(task, catalog, facts, extracted, basis)
        if gate_result:
            return gate_result

        has_premier_path = any(
            rule.required and rule.rule_type == "banking_relationship"
            for product in gate_products for rule in product.eligibility_rules
        )
        if (not request.refresh_mode and not adaptive_hk and has_premier_path and facts.get("hsbc_premier_path") is None
                and facts.get("premier_relationship") is None):
            return self._needs_input(
                task, request, extracted, basis, ["hsbc_premier_path"],
                {"hsbc_premier_path": (
                    "有些卡需要汇丰卓越理财关系。方便了解以下哪种情况比较接近您吗？"
                    "这里的一百万港元指在汇丰的总关系结余，不是年收入。"
                )},
                QUESTION_OPTIONS,
            )

        if (not request.refresh_mode and not adaptive_hk and facts.get("premier_relationship") is True
                and facts.get("hsbc_total_relationship_balance_hkd") is None
                and facts.get("hsbc_relationship_balance_band") is None):
            return self._needs_input(
                task, request, extracted, basis, ["hsbc_relationship_balance_band"],
                {"hsbc_relationship_balance_band": (
                    "谢谢您说明已有汇丰卓越理财关系。为了核对 Premier Mastercard 的申请条件，"
                    "方便再选一下您在汇丰的总关系结余区间吗？"
                )},
                QUESTION_OPTIONS,
            )

        needs_asset = any(needs_asset_information(product, facts) for product in gate_products)
        if not request.refresh_mode and not adaptive_hk and needs_asset and facts.get("asset_band") is None:
            return self._needs_input(
                task, request, extracted, basis, ["asset_band"],
                {"asset_band": (
                    "部分卡还可以由银行参考资产资料作个案评估。如果您愿意走这条路径，"
                    "方便了解您是否能提供最近 3 个月的银行资产证明，以及大致资产区间吗？"
                    "最后仍由银行个案审批。"
                )},
                QUESTION_OPTIONS,
            )

        required_usage_fields = ["repayment_mode", "spending_scenarios"]
        scenarios_so_far = facts.get("spending_scenarios") or []
        if isinstance(scenarios_so_far, str):
            scenarios_so_far = [item for item in scenarios_so_far.split(",") if item]
        if "mainland_china" in scenarios_so_far:
            required_usage_fields.append("mainland_payment_method")
        required_usage_fields += ["monthly_card_spend_band_hkd", "reward_preference"]
        missing = [field for field in required_usage_fields if not facts.get(field)]
        if missing and not adaptive_hk and not request.refresh_mode:
            prompts = {
                "repayment_mode": "为了把回赠和借款成本一起算进去，方便了解您通常会每月全额还款，还是可能分期或保留余额吗？",
                "spending_scenarios": (
                    "想把卡片和您的日常用途对得更准一些。方便选出最常用的两三个消费场景吗？"
                    "例如网购、餐饮、旅行、内地消费或学费；先选的我会看得更重。"
                ),
                "monthly_card_spend_band_hkd": (
                    "为了估算回赠能否抵得上年费和外币手续费，方便了解您每月刷卡消费大约在哪个区间吗？选范围即可。"
                ),
                "reward_preference": "您会比较喜欢现金回赠、积分或航空里程吗？如果暂时没有偏好，我就按预计价值帮您比较。",
                "mainland_payment_method": (
                    "您提到会在内地消费，方便了解您平时更常用手机或二维码，还是刷实体卡吗？"
                    "这会影响实际回赠；例如 Pulse 的 4.4% 是组合上限，还要符合付款方式和自选类别条件。"
                ),
            }
            return self._needs_input(task, request, extracted, basis, missing, prompts, QUESTION_OPTIONS)

        provisional_fields = [field for field in ("applicant_age_band", "annual_income_band_hkd",
                              "hk_resident", "repayment_mode", "spending_scenarios",
                              "monthly_card_spend_band_hkd")
                              if adaptive_hk and (facts.get(field) is None if field == "hk_resident"
                                                  else not facts.get(field))
                              and not (field == "hk_resident" and not requires_residency)
                              and not (field == "applicant_age_band" and facts.get("applicant_age") is not None)
                              and not (field == "annual_income_band_hkd" and facts.get("annual_income_hkd") is not None)]
        if adaptive_hk or request.refresh_mode:
            if not facts.get("repayment_mode"):
                facts["repayment_mode"] = "full"
            if not facts.get("spending_scenarios"):
                facts["spending_scenarios"] = []
        borrowing_products = catalog.products
        if facts["repayment_mode"] != "full" and not all(
                any(term.term_type == kind for term in product.borrowing_terms)
                for product in borrowing_products
                for kind in ("purchase_apr", "interest_free_period", "spending_instalment")):
            return DomainResult(
                task_id=task.task_id, domain="credit_card", status="needs_verification",
                answer=AnswerBody(
                    summary="如果您可能分期或保留余额，应优先比较借款成本；目前这部分资料还不完整。",
                    limitations=["借款成本资料补齐前，不按返现或积分给出确定推荐。"],
                ),
                missing_fields=["card_apr_data"],
                next_actions=["补齐信用卡年利率、分期手续费和免息期数据后重新比较。"],
                notices=[Notice(code="borrowing_cost_data_missing", message="信用卡借款成本资料缺失")],
                observed_facts=extracted, observed_basis=basis,
            )

        scenarios = facts["spending_scenarios"] if isinstance(facts["spending_scenarios"], list) else \
            [item for item in str(facts["spending_scenarios"]).split(",") if item]
        # 学费场景 → 核对学校官方缴费通道（见 _tuition_payment_check）。
        tuition_check = self._tuition_payment_check(facts, scenarios)
        if tuition_check:
            scenarios = tuition_check["scenarios"]
        result = recommend(catalog.products, facts, scenarios,
                           facts.get("reward_preference", "no_preference"), now=self.clock())
        if result.primary is None:
            names = {product.card_id: product.product_name for product in catalog.products}
            steps = [
                f"{names.get(card_id, '汇丰信用卡')}：{'；'.join(_public_reason(reason) for reason in reasons)}"
                for card_id, reasons in result.excluded.items()
            ]
            dual_hurdle = facts.get("hk_resident") is False and any(
                card.eligibility.matched_path == "asset_review"
                for card in result.ranked_candidates)
            summary = (
                "您目前不符合香港居民的成文申请条件，收入也未达到相关卡片的公开门槛。"
                "仅靠提交资产资料接受个案审批，获批把握不足，我暂时不建议您申请某张特定信用卡。"
                "如果申请条件之后有变化，我可以重新为您比较。"
                if dual_hurdle else
                "根据目前已确认的申请条件，暂时没有可推荐的汇丰信用卡。"
                "若之后情况有变化，我们可以再为您比较适合的选择。")
            return DomainResult(
                task_id=task.task_id, domain="credit_card", status="answered",
                answer=AnswerBody(
                    summary=summary,
                    steps=steps),
                payload={"recommendations": result.model_dump(mode="json"),
                         "catalog": self._catalog_summary(catalog)},
                knowledge_read_id=catalog.read_id, observed_facts=extracted, observed_basis=basis,
            )

        near_tie_ids = set()
        if adaptive_hk and scenarios:
            result, near_tie_ids = self._hk_include_near_ties(result)
        eligibility_missing = list(dict.fromkeys(
            field for card in [result.primary, *result.supplements]
            for field in card.eligibility.missing_fields)) if adaptive_hk else []
        optional_inputs = [field for field in provisional_fields
                           if field in {"repayment_mode", "spending_scenarios",
                                        "monthly_card_spend_band_hkd"}]
        provisional_fields = list(dict.fromkeys([*eligibility_missing, *optional_inputs]))
        shown_ids = {result.primary.card_id, *(card.card_id for card in result.supplements)}
        conditional_choices = [card for card in result.conditional_alternatives
                               if card.card_id not in shown_ids][:max(0, 2 - len(result.supplements))]
        fee_choices = ([] if conditional_choices else
                       result.alternatives[:max(0, 2 - len(result.supplements))])
        recommendations = [result.primary, *result.supplements, *conditional_choices,
                           *fee_choices]
        provisional_choice = bool(provisional_fields) or result.primary.eligibility.status != "eligible"
        now = self.clock()
        by_id = {product.card_id: product for product in catalog.products}
        product_data_ready = all(
            card.official_url
            and by_id[card.card_id].last_verified_at
            and now - by_id[card.card_id].last_verified_at <= timedelta(days=30)
            and all(item.source_url and item.last_verified_at
                    for item in [*by_id[card.card_id].reward_rules,
                                 *by_id[card.card_id].fees,
                                 *by_id[card.card_id].benefits])
            for card in recommendations
        )
        show_estimates = not (adaptive_hk and "monthly_card_spend_band_hkd" in provisional_fields)
        primary_product = by_id.get(result.primary.card_id)
        student_card_confirmed = bool(
            primary_product is not None
            and any(rule.rule_type == "student_status"
                    for rule in primary_product.eligibility_rules)
            and not ({"full_time_student", "student_institution_eligible"}
                     & set(result.primary.eligibility.missing_fields)))
        steps = self._format_card_lines(result.primary, show_estimates=show_estimates,
                                        provisional=provisional_choice,
                                        student_card_confirmed=student_card_confirmed)
        for card in result.supplements:
            steps.extend(self._format_card_lines(card, show_estimates=show_estimates,
                                                 provisional=provisional_choice,
                                                 near_tie=card.card_id in near_tie_ids))
        for card in conditional_choices:
            steps.extend(self._format_card_lines(card, show_estimates=False, provisional=True))
            floor = min((rule.value_numeric for rule in by_id[card.card_id].eligibility_rules
                         if rule.required and rule.rule_type == "minimum_income"
                         and rule.value_numeric is not None), default=None)
            if floor is not None:
                band = str(facts.get("annual_income_band_hkd") or "").split("_")
                crosses = (len(band) >= 3 and band[0] == "range"
                           and Decimal(band[1]) < floor <= Decimal(band[2]))
                steps.append(
                    f"申请前需核对：该卡公开的年收入门槛为{floor:,.0f}港元；"
                    + ("您填写的区间跨过这一门槛，需核对实际收入及其他申请条件。"
                       if crosses else "按您填写的收入，已达到这一门槛；其他申请条件仍需核对。"))
        if facts["repayment_mode"] != "full" and not (adaptive_hk and "repayment_mode" in provisional_fields):
            steps.insert(0, self._format_borrowing_costs(by_id[result.primary.card_id]))
        if fee_choices:
            steps.append("免年费选择：" + "、".join(card.product_name for card in fee_choices))
        # Customer-manager advice is strictly an explanation layer.  A failure
        # to read supplementary account data must not interrupt card advice.
        try:
            advisor_brief = (await self.advisor.enrich(
                result.primary, by_id[result.primary.card_id], facts, recommendations)
                if not (adaptive_hk and provisional_choice) else None)
        except Exception:
            advisor_brief = None
        if advisor_brief:
            steps.extend(advisor_brief.steps)
        limitations = list(dict.fromkeys(
            limit for card in recommendations for limit in card.limitations
            if limit and not limit.startswith(("满足", "不满足", "缺少门槛", "非香港居民"))))
        if provisional_fields:
            limitations.insert(0, (
                "这是按目前资料筛出的初步选择；尚缺少部分申请资格信息，申请前需核对，最终由银行审批。"
                if eligibility_missing else
                "这是按目前资料给出的初步选择。您之后若补充用卡习惯，我可以据此更新推荐。"))
            if "monthly_card_spend_band_hkd" in provisional_fields:
                limitations.append("未提供每月消费额，本次不展示回赠金额估算；之后补充消费范围即可更新比较。")
            if "repayment_mode" in provisional_fields:
                limitations.append("未提供还款习惯时，本次按每月全额还款比较奖赏；若之后考虑分期或保留余额，我可以重新比较借款成本。")
            if "spending_scenarios" in provisional_fields:
                limitations.append("如以后告诉我常用的消费场景，我可以按您的实际用途调整推荐。")
            if "applicant_age_band" in provisional_fields or "applicant_age" in provisional_fields:
                limitations.append("尚未确认申请年龄；申请前需核对是否达到该卡的年龄要求。")
            if "annual_income_band_hkd" in provisional_fields or "annual_income_hkd" in provisional_fields:
                limitations.append("尚未确认收入条件；申请前需核对该卡的公开门槛及可用的替代申请路径。")
            if "hk_resident" in provisional_fields:
                limitations.append("尚未确认香港居民身份；汇丰香港卡的成文申请条件需在申请前核对。")
            if any(field in provisional_fields for field in (
                    "full_time_student", "student_institution_eligible")):
                limitations.append("若考虑学生卡，还需核对全日制学生身份及是否就读指定院校。")
            if any(field in provisional_fields for field in (
                    "hsbc_premier_path", "premier_relationship", "hsbc_relationship_balance_band")):
                limitations.append("涉及汇丰卓越理财关系的卡片，还需核对客户资格及总关系结余。")
        if conditional_choices:
            limitations.append("另列的普通卡尚有申请条件未确认，只供您比较；申请前需核对实际收入、居住地和账户条件。")
        if adaptive_hk and not facts.get("asset_band") and any(
                needs_asset_information(by_id[card.card_id], facts) for card in recommendations):
            limitations.append("若收入未达公开门槛，可向汇丰了解能否提交最近三个月银行资产证明作个案审批；这不保证获批。")
        # Non-resident applicants: HSBC lists HK residency as a written basic
        # requirement, yet a small number of cases do get approved. The cards
        # stay recommended with a prominent low-approval-chance disclosure.
        if any(card.eligibility.low_approval_chance for card in recommendations):
            limitations.insert(0, (
                "您不是香港居民：汇丰香港成文条件要求申请人一般为香港居民，"
                "获批几率很低，以下推荐仅为参考，最终由银行审批。"))
        if not product_data_ready:
            limitations.insert(0, "部分年费或奖赏资料仍待官网核验，请以发卡机构页面为准。")
        # Time-limited promotional layers must be disclosed, not buried in a rate.
        promo_notes = list(dict.fromkeys(
            PROMOS_ZH.get(note, "其他限时推广条款请见官网")
            for card in recommendations for note in card.promo_rules))
        if promo_notes:
            limitations.append(
                "估算含以下限时推广奖赏，推广期结束后该部分按基础率计算："
                + "；".join(promo_notes) + "。")
        # Rewards of Your Choice caps are shared per HKID across cards, so a
        # multi-card combination must not double-count the accelerated layer.
        combo = [result.primary, *result.supplements]
        yc_cards = [card for card in combo if any(
            rule.qualification_type == "selected_category"
            for rule in by_id[card.card_id].reward_rules)]
        if len(yc_cards) > 1:
            limitations.append(
                "「最红自主奖赏」自选类别的每年首十万港元加速奖赏以身份证合计、"
                "跨卡共享；多卡组合时不能各自再享一次上限。")
        limitations.append("目前仅覆盖汇丰香港信用卡，暂不比较其他发卡机构的产品。")
        limitations.append(
            "迎新礼遇（如首年额外现金回赠或飞行里程）属一次性优惠，未计入年度净值估算；"
            "申请前可在官网查看最新迎新条款。")
        if tuition_check:
            # The school's payment-channel reality leads the answer, ahead of
            # any card lines, so a forbidden channel is never buried.
            steps[:0] = tuition_check["steps"]
            limitations.extend(tuition_check["limitations"])
        evidence = list(dict.fromkeys(
            [card.official_url for card in recommendations if card.official_url]
            + [source for card in recommendations for source in card.borrowing_sources]
            + (advisor_brief.evidence_refs if advisor_brief else [])))
        next_actions = ["申请前在发卡机构官网确认资格、年费、奖励上限和最新条款。"]
        if advisor_brief and advisor_brief.next_action:
            next_actions.insert(0, advisor_brief.next_action)
        return DomainResult(
            task_id=task.task_id, domain="credit_card",
            status="answered" if product_data_ready else "needs_verification",
            answer=AnswerBody(summary=f"{'初步选择' if provisional_choice else '主推荐'}：{result.primary.product_name}", steps=steps,
                              limitations=limitations),
            evidence_refs=evidence,
            next_actions=list(dict.fromkeys(next_actions)),
            notices=[] if product_data_ready else [Notice(
                code="card_product_fields_need_verification",
                message="信用卡奖励和年费字段尚未逐项完成官方核验")],
            payload={"recommendations": result.model_dump(mode="json"), "market": catalog.market,
                     "provisional": bool(provisional_fields),
                     "unconfirmed_fields": eligibility_missing,
                     "optional_inputs": optional_inputs,
                     "tuition_payment_check": (
                         {key: tuition_check[key] for key in ("school", "payment", "scenarios")}
                         if tuition_check else None),
                     "catalog": self._catalog_summary(catalog),
                     "advisor": advisor_brief.audit if advisor_brief else {
                         "policy_version": "credit-card-advisor-v1", "suppressed": "unavailable"}},
            knowledge_read_id=catalog.read_id, observed_facts=extracted, observed_basis=basis,
        )

    async def _product_question(self, task, request, facts, extracted, basis):
        context = request.domain_state.get("credit_card") or {}
        region = (facts.get("application_region") if extracted.get("application_region")
                  else context.get("market") or facts.get("application_region"))
        markets = list(STUDY_MARKETS.get(region, (region,))) if region else ["CN-HK", "UK", "US"]
        catalogs = []
        for market in dict.fromkeys(market for market in markets if market):
            try:
                catalogs.append(await self.repository.active_products(market, self.clock()))
            except CreditCardRepositoryError:
                continue
        products = [product for catalog in catalogs for product in catalog.products]
        named = _named_card_products(request.text, products)
        contextual_reference = _refers_to_card(request.text) or bool(
            context.get("cards") and re.fullmatch(r"(?:那|那么|还有)?(?:年费|费用|权益|回赠|积分|里程)(?:呢|多少|怎么样)?[？?]?", request.text.strip()))
        if not named and not contextual_reference:
            # Explicit product names outrank the study destination or the
            # previously recommended market. Search the other shelves only
            # when the current shelf did not resolve the names.
            for market in ("CN-HK", "UK", "US"):
                if market in markets:
                    continue
                try:
                    catalog = await self.repository.active_products(market, self.clock())
                except CreditCardRepositoryError:
                    continue
                catalogs.append(catalog)
                products.extend(catalog.products)
            named = _named_card_products(request.text, products)
        if not named and contextual_reference:
            cards = context.get("cards") or []
            focus = next((card for card in cards if card.get("product_name")
                          and card["product_name"] in request.session_last_assistant_text), None)
            if "另一张" in request.text or "备选卡" in request.text:
                preferred = next((card for card in cards
                                  if card.get("role") != "primary"
                                  and card.get("card_id") != (focus or {}).get("card_id")), None)
            else:
                preferred = focus or next((card for card in cards
                                           if card.get("role") == "primary"), None)
                preferred = preferred or (cards[0] if cards else None)
            if preferred:
                named = [product for product in products
                         if product.card_id == preferred.get("card_id")
                         and product.product_name == preferred.get("product_name")]
            if not named:
                return DomainResult(
                    task_id=task.task_id, domain="credit_card", status="answered",
                    answer=AnswerBody(summary="您说的是哪张卡？请告诉我产品名称，我会查它的具体条款。"),
                    payload={"capability": "product_qa"}, projection="chat_only",
                    observed_facts=extracted, observed_basis=basis)
        if not named:
            return None

        if len(named) == 1 and re.search(r"年费|豁免年费|免年费", request.text):
            product = named[0]
            annual = next((fee for fee in product.fees if fee.fee_type == "annual_fee"), None)
            if annual is None or annual.amount is None:
                fee_line = "目前的产品资料没有已核实的年费金额。"
            elif annual.amount == 0:
                fee_line = "年费：免年费。"
            else:
                fee_line = f"年费：{_money(annual.amount, annual.currency)}。"
            if annual and annual.amount == 0:
                waiver_line = "这张卡目前标示免年费，无需满足消费额豁免条件。"
            elif annual and annual.waiver_condition:
                waiver_line = f"豁免条件：{_waiver_text(annual.waiver_condition)}。"
            else:
                waiver_line = "目前资料没有已核实的年费豁免条件，申请前请查发卡行最新条款。"
            source = (annual.source_url if annual and annual.source_url else product.official_url)
            # Older imports attached another card's welcome pack to some fee rows.
            if source and "/credit-cards/red/" in source and product.card_id != "hsbc_red":
                source = product.official_url
            steps = [fee_line, waiver_line]
            if annual and annual.amount and annual.waiver_condition in {
                    "First two years waived", "First two years annual fee waiver"}:
                steps.append(f"首两年后按目前资料年费为{_money(annual.amount, annual.currency)}；"
                             "其他豁免条件请以发卡行最新条款为准。")
            sources = [source] if source else []
            if (annual and annual.waiver_condition and product.official_url
                    and product.official_url not in sources):
                sources.append(product.official_url)
            if sources:
                steps.append("资料来源：" + "；".join(sources))
            return DomainResult(
                task_id=task.task_id, domain="credit_card",
                status="answered" if source and annual and annual.amount is not None else "needs_verification",
                answer=AnswerBody(
                    summary=f"{product.product_name} 的年费与豁免条件如下。",
                    steps=steps,
                    limitations=["费用与豁免条款可能调整，申请前请核对发卡行官网。"]),
                evidence_refs=sources,
                payload={"capability": "product_qa", "products": [{
                    "card_id": product.card_id, "product_name": product.product_name,
                    "market": product.market}]},
                projection="chat_only", knowledge_read_id="+".join(
                    catalog.read_id for catalog in catalogs),
                observed_facts=extracted, observed_basis=basis)

        lines = []
        evidence = []
        product_payload = []
        for product in named:
            assessment = assess_eligibility(product, facts)
            categories = list(dict.fromkeys(
                _category_text(rule.spend_category)
                for rule in product.reward_rules if rule.spend_category))
            fee = self._fee_text_from_product(product)
            perks = self._perks_from_product(product)
            if len(named) > 1:
                lines.append(f"## {product.product_name}")
            lines.extend([
                f"产品特点：{'、'.join(categories[:4]) if categories else '日常消费'}相关奖赏；{perks}。",
                f"年费及费用：{fee}。",
            ])
            restriction = self._restriction_text(assessment)
            if restriction:
                lines.append(f"申请条件：{restriction}。")
            profile_scenarios = facts.get("spending_scenarios") or []
            if isinstance(profile_scenarios, str):
                profile_scenarios = [profile_scenarios]
            raw_categories = {rule.spend_category for rule in product.reward_rules}
            matched = [_category_text(item) for item in profile_scenarios
                       if item in raw_categories
                       or (item == "mainland_china" and "mainland_china_and_macau" in raw_categories)]
            if matched:
                lines.append(f"结合您的情况：它与您关注的{'、'.join(matched)}比较匹配。")
            elif categories:
                lines.append(f"我的看法：如果您主要用于{'、'.join(categories[:2])}，这张卡值得比较；"
                             "若消费重点不同，不应只看宣传回赠率。")
            sources = list(dict.fromkeys(
                [product.official_url] +
                [item.source_url for item in [*product.reward_rules, *product.fees,
                                              *product.benefits, *product.eligibility_rules]
                 if item.source_url]))
            evidence.extend(source for source in sources if source)
            product_payload.append({
                "card_id": product.card_id, "product_name": product.product_name,
                "market": product.market, "annual_fee_text": fee,
                "benefits_text": perks, "eligibility": assessment.model_dump(mode="json"),
            })

        if len(named) == 2:
            first, second = named
            first_categories = {rule.spend_category for rule in first.reward_rules}
            second_categories = {rule.spend_category for rule in second.reward_rules}
            first_unique = [_category_text(code) for code in sorted(first_categories - second_categories)]
            second_unique = [_category_text(code) for code in sorted(second_categories - first_categories)]
            if first_unique and second_unique:
                lines.append(
                    f"怎么选：如果主要用于{first_unique[0]}，可以先看 {first.product_name}；"
                    f"如果主要用于{second_unique[0]}，可以先看 {second.product_name}。"
                    "具体回赠仍要核对合资格交易和上限。")
            first_fee, second_fee = self._study_annual_fee(first), self._study_annual_fee(second)
            if first_fee and second_fee and first_fee.amount != second_fee.amount:
                lower = first if first_fee.amount < second_fee.amount else second
                lines.append(f"若您暂时没有明确偏好的使用场景，{lower.product_name} 的公布年费较低；"
                             "另一张卡是否更合适，还要结合年费豁免条件与您的实际使用频率。")

        summary = (f"{named[0].product_name} 的费用、权益和适用场景如下。"
                   if len(named) == 1 else
                   f"我按费用、权益和申请条件比较 {named[0].product_name} 与 {named[1].product_name}。")
        return DomainResult(
            task_id=task.task_id,
            domain="credit_card",
            status="answered" if evidence else "needs_verification",
            answer=AnswerBody(
                summary=summary,
                steps=lines,
                limitations=["产品条款和推广可能变化，申请前请再核对发卡机构官网。"],
            ),
            evidence_refs=list(dict.fromkeys(evidence)),
            payload={"capability": "product_qa",
                     "products": product_payload},
            projection="chat_only",
            knowledge_read_id="+".join(catalog.read_id for catalog in catalogs),
            observed_facts=extracted,
            observed_basis=basis,
        )

    async def _run_study(self, task, request, facts, extracted, basis):
        """UK/US study path: local HSBC cards first, Hong Kong shelf as backup.

        Do not reuse Hong Kong income / Premier TRB / designated-school questions
        for the local shelf; HK cards stay a separate low-approval backup group.
        """
        region = facts["application_region"]
        if (not request.refresh_mode and region == "US"
                and facts.get("us_hsbc_account_path") == "need_opening_guidance"):
            return await self._us_checking_first(task, facts, extracted, basis)
        try:
            catalogs = await self._catalogs(region, facts.get("card_query_scope"))
        except CreditCardRepositoryError:
            return self._repository_unavailable(task, extracted, basis)
        local_catalog = next((item for market, item in catalogs if market == region), None)
        if local_catalog is None or not local_catalog.products:
            combined = self._combined(catalogs)
            if not combined.products:
                return self._empty_catalog(task, combined, extracted, basis, facts.get("card_query_scope"))

        # A confirmed age restriction is enough to stop; do not ask for a
        # local account (or spending) if every local product is already ruled out.
        underage = facts.get("applicant_age_band") == "under_18" or (
            facts.get("applicant_age") is not None and facts["applicant_age"] < 18)
        if (underage and local_catalog and local_catalog.products
                and all(assess_eligibility(product, facts).status == "ineligible"
                        for product in local_catalog.products)):
            return self._study_answer(task, catalogs, facts, [], extracted, basis)

        account_field = "us_hsbc_account_path" if region == "US" else "uk_hsbc_account_path"
        if facts.get(account_field) is None and not request.refresh_mode:
            # Premier in another country does not establish a local account.
            prompt = (
                "为了按您实际可申请的路径比较，方便了解您是否已有英国汇丰学生账户、卓越理财或私人银行账户吗？"
                if region == "UK" else
                "为了避免推荐暂时无法申请的美国卡，方便了解您是否已有"
                "美国汇丰卓越理财支票账户吗？"
            )
            return self._needs_input(
                task, request, extracted, basis, [account_field],
                {account_field: prompt},
                QUESTION_OPTIONS)

        # Without UK Premier, lead with the Student Card and disclose that a
        # UK Student Bank Account must be opened first. No second question can
        # improve that recommendation.
        if region == "US" and facts.get(account_field) in {"plan_to_open", "not_applicable"}:
            # Planned account: introduce the local cards as future choices,
            # without suggesting they can be applied for yet. No account plan:
            # do not display cards with unmet hard prerequisites.
            return self._study_answer(task, catalogs, facts, [], extracted, basis)
        assessments = [assess_eligibility(product, facts)
                       for product in local_catalog.products]
        if assessments and all(item.status == "ineligible" for item in assessments):
            # An explicit application gate has already ruled out this shelf.
            # Do not ask how the customer spends before explaining that gate.
            return self._study_answer(task, catalogs, facts, [], extracted, basis)
        if (not request.refresh_mode and (region == "US" or facts.get(account_field) == "premier_account")
                and facts.get("applicant_age") is None
                and facts.get("applicant_age_band") is None
                and any("applicant_age" in item.missing_fields
                        for item in assessments)):
            return self._needs_input(
                task, request, extracted, basis, ["applicant_age_band"],
                {"applicant_age_band": (
                    "方便了解您是否已满 18 岁吗？确认申请条件后，我再按您的使用需求挑选卡片。"
                )}, QUESTION_OPTIONS)
        decision_pair = self._study_decision_pair(local_catalog.products, facts)
        if decision_pair and not facts.get("study_card_focus") and not request.refresh_mode:
            lower, premium = decision_pair
            return self._needs_input(
                task, request, extracted, basis, ["study_card_focus"],
                {"study_card_focus": (
                    f"{lower.product_name}年费{self._study_annual_fee_label(lower)}；"
                    f"{premium.product_name}年费{self._study_annual_fee_label(premium)}。"
                    "您会更在意控制年费，还是想多一些旅行权益？也可以先看看两张卡的对比。"
                )}, QUESTION_OPTIONS)

        raw_scenarios = facts.get("spending_scenarios") or []
        scenarios = raw_scenarios if isinstance(raw_scenarios, list) else \
            [item for item in str(raw_scenarios).split(",") if item]
        # 学费场景 → 核对学校官方缴费通道（见 _tuition_payment_check）。
        tuition_check = self._tuition_payment_check(facts, scenarios)
        if tuition_check:
            scenarios = tuition_check["scenarios"]
        return self._study_answer(task, catalogs, facts, scenarios, extracted, basis,
                                  tuition_check=tuition_check)

    @staticmethod
    def _load_us_premier_opening_info() -> dict | None:
        """Read US Premier Checking opening facts from the shared RDS account tables.

        Returns None (never raises) when the database is unreachable or the
        core rows are missing, so the customer still gets generic guidance
        instead of a failed answer. Every number quoted downstream comes from
        a verified official row, never from memory.
        """
        account_id = "acct_us_premier"
        try:
            from agent1.storage import read_session
            with read_session() as db:
                age = db.execute(
                    "SELECT value_numeric FROM ACCOUNT_ELIGIBILITY_RULE "
                    "WHERE account_id=%s AND rule_type='minimum_age'", (account_id,)).fetchone()
                one_of = db.execute(
                    "SELECT value_text FROM ACCOUNT_ELIGIBILITY_RULE "
                    "WHERE account_id=%s AND rule_type='other' AND operator='one_of'",
                    (account_id,)).fetchone()
                fee = db.execute(
                    "SELECT amount, currency, waiver_condition FROM ACCOUNT_FEE "
                    "WHERE account_id=%s AND fee_type='monthly_fee'", (account_id,)).fetchone()
                routes = db.execute(
                    "SELECT application_channel, can_apply_before_arrival, branch_visit_required, "
                    "additional_notes FROM ACCOUNT_OPENING_ROUTE WHERE account_id=%s",
                    (account_id,)).fetchall()
                docs = db.execute(
                    "SELECT document_type, document_name, required, condition_note "
                    "FROM ACCOUNT_REQUIRED_DOCUMENT WHERE account_id=%s ORDER BY requirement_id",
                    (account_id,)).fetchall()
                verified = db.execute(
                    "SELECT MAX(last_verified_at) AS verified FROM ACCOUNT_PRODUCT "
                    "WHERE account_id=%s", (account_id,)).fetchone()
        except Exception:
            return None
        if fee is None or not routes:
            return None

        def has_cjk(text: str) -> bool:
            return any("\u4e00" <= ch <= "\u9fff" for ch in text)

        # The Premier relationship routes that keep the monthly fee waived.
        waiver_routes = []
        if one_of and one_of["value_text"]:
            for part in one_of["value_text"].split(";"):
                part = part.strip()
                if not part:
                    continue
                low = part.lower()
                match = re.search(r"USD\s*([\d,]+)", part)
                if "relationship balance" in low:
                    waiver_routes.append(
                        f"合格存款与投资结余达 {_usd_amount_zh(match.group(1))}美元"
                        if match else "保持官方公布的合格存款与投资结余")
                elif "direct deposit" in low:
                    waiver_routes.append(
                        f"每月合格直接入账达 {_usd_amount_zh(match.group(1))}美元"
                        if match else "每月合格直接入账达官方规定金额")
                elif "mortgage" in low:
                    waiver_routes.append("持有合资格的汇丰美国按揭")
                elif "private bank" in low:
                    waiver_routes.append("成为汇丰私人银行客户")
                else:
                    waiver_routes.append(part)

        notes = []
        for route in routes:
            note = (route["additional_notes"] or "").strip()
            if not note:
                continue
            notes.append("联名申请或超出线上标准的情况，银行可能需要电话回访补充资料。"
                         if "Joint applications" in note else note)

        # Bilingual rows exist for the same logical document; keep the Chinese
        # name when both are present, one entry per document type.
        required_docs: dict[str, str] = {}
        optional_docs: dict[str, str] = {}
        passport_validity = False
        for doc in docs:
            name = (doc["document_name"] or "").strip()
            if not name:
                continue
            bucket = required_docs if doc["required"] else optional_docs
            key = doc["document_type"]
            current = bucket.get(key)
            if current is None or (has_cjk(name) and not has_cjk(current)):
                bucket[key] = name
            if "six months" in (doc["condition_note"] or ""):
                passport_validity = True

        return {
            "min_age": (int(age["value_numeric"])
                        if age and age["value_numeric"] is not None else None),
            "waiver_routes": waiver_routes,
            "fee_amount": str(fee["amount"]),
            "fee_waiver": (fee["waiver_condition"] or "").strip() or None,
            "channels": sorted({route["application_channel"] for route in routes}),
            "before_arrival": any(bool(route["can_apply_before_arrival"]) for route in routes),
            "branch_visit_required": any(bool(route["branch_visit_required"]) for route in routes),
            "route_notes": [note.rstrip("。") for note in dict.fromkeys(notes)],
            "required_documents": list(required_docs.values()),
            "optional_documents": list(optional_docs.values()),
            "passport_validity": passport_validity,
            "verified_at": (verified["verified"].strftime("%Y-%m-%d")
                             if verified and verified["verified"] else None),
        }

    async def _us_checking_first(self, task, facts, extracted, basis):
        """Guide a customer without Premier Checking through the prerequisite first."""
        underage = facts.get("applicant_age_band") == "under_18" or (
            facts.get("applicant_age") is not None and facts["applicant_age"] < 18)
        info = await anyio.to_thread.run_sync(self._load_us_premier_opening_info)
        if info:
            summary = ("先把美国汇丰 Premier Checking 开好，美国信用卡才有申请基础；"
                       "我把官方核验过的开户条件、费用和流程一次讲清楚。")
            eligibility = (f"开户资格：申请人须年满 {info['min_age']} 岁。"
                           if info["min_age"] else
                           "开户资格：申请人须达到官方公布的最低开户年龄。")
            if info["waiver_routes"]:
                eligibility += ("开户后维持 Premier 关系可免账户月费，满足以下任一条件即可："
                                + "；".join(info["waiver_routes"]) + "。")
            steps = [eligibility]
            fee_text = (f"账户月费：每月 {_usd_amount_zh(info['fee_amount'])} 美元。"
                        if info["fee_amount"] else "账户月费：以官方公布标准为准。")
            if info["fee_waiver"]:
                fee_text += info["fee_waiver"]
            steps.append(fee_text)
            route_text = "申请方式：" + "、".join(
                "线上" if channel == "online" else channel for channel in info["channels"])
            if not info["branch_visit_required"]:
                route_text += "申请，无需跑分行"
            if not info["before_arrival"]:
                route_text += "；汇丰美国目前不接受抵美前远程开户，请抵美后再提交申请"
            if info["route_notes"]:
                route_text += "；" + "；".join(info["route_notes"])
            steps.append(route_text + "。")
            if info["required_documents"] or info["optional_documents"]:
                doc_text = "准备材料：" + "；".join(
                    info["required_documents"] + info["optional_documents"])
                if info["passport_validity"]:
                    doc_text += "；如用护照开户，剩余有效期建议至少 6 个月"
                steps.append(doc_text + "。")
            steps.append("账户开好后，告诉我「已开好美国汇丰 Premier Checking」，"
                         "我马上按您的消费场景比较美国汇丰信用卡的年费、权益和申请条件。")
            limitations = [
                "美国汇丰信用卡需要另行申请并由银行审批；开好支票账户不保证信用卡获批。",
                "以上开户条件与费用整理自汇丰美国官方公布信息"
                + (f"（{info['verified_at']} 核验）" if info["verified_at"] else "")
                + "，条款如有更新以官网最新说明为准。",
            ]
        else:
            # RDS unreachable: generic guidance only, no numbers from memory.
            summary = "我们先看美国汇丰 Premier Checking 怎么开；账户开立后，再为您比较信用卡。"
            steps = [
                "先查看汇丰美国 Premier Checking 官方页面，核对年龄、申请所在地、卓越理财资格和账户费用条件。",
                "符合条件后，按官网指引选择线上申请或联系银行协助，准备银行要求的身份证明、地址及税务资料。",
                "账户成功开立后，告诉我「已开好美国汇丰 Premier Checking」，我再按您的用途比较当地信用卡的年费、权益和申请条件。",
            ]
            limitations = [
                "美国汇丰信用卡需要另行申请并由银行审批；开好支票账户不保证信用卡获批。",
                "Premier Checking 的开户资格、费用和申请方式请以汇丰美国官网最新说明为准。",
            ]
        if underage:
            limitations.insert(0, "您目前未满 18 岁，尚未达到 Premier Checking 官网列出的最低开户年龄。")
        return DomainResult(
            task_id=task.task_id, domain="credit_card", status="answered",
            answer=AnswerBody(
                summary=summary,
                steps=steps,
                limitations=limitations,
            ),
            evidence_refs=[US_PREMIER_CHECKING_URL, US_CARDS_URL],
            next_actions=["按上述条件准备材料，抵美后完成 Premier Checking 开户；"
                          "开好后再来比较信用卡。"] if info
            else ["先核对并申请美国汇丰 Premier Checking；开户成功后再比较信用卡。"],
            payload={"groups": [], "market": "US", "journey": "premier_checking_first",
                     "checking_url": US_PREMIER_CHECKING_URL,
                     "opening_info_source": "rds_account_tables" if info else "unavailable"},
            observed_facts=extracted, observed_basis=basis,
        )

    @staticmethod
    def _tuition_payment_check(facts: dict, scenarios: list[str]) -> dict | None:
        """Verify the 学费 scenario against the bound school's official channels.

        The user declared 学费 as a card usage scenario; TUITION_PAYMENT_DETAIL
        records how that school actually collects fees. Ranking must not
        reward a tuition programme the school's channel forbids, and the
        reality (cards accepted / channel-only / not recorded) is stated
        outright. Returns adjusted scenarios + steps + limitations, or None
        when the tuition scenario or the school fact is absent / the catalog
        is unreachable (deterministic ranking is kept either way).
        """
        if "tuition" not in scenarios:
            return None
        school = str(facts.get("school") or "").strip()
        if not school:
            return None
        try:
            ctx = resolve_tuition(
                school=school,
                course=(str(facts["course"]) if facts.get("course") else None),
                destination_country=(str(facts["destination_country"])
                                    if facts.get("destination_country") else None))
        except Exception:
            return None
        if not ctx.school_name:
            return None
        scenarios = list(scenarios)
        steps: list[str] = []
        limitations: list[str] = []
        payment = ctx.payment
        if payment is None:
            steps.append(f"已按您登记的学校核对：暂未收录 {ctx.school_name} 的官方缴费方式；"
                         "学费能否以信用卡缴纳，请以学校缴费页面为准。")
            limitations.append("学费场景按一般境外消费估算；如需按学校官方缴费通道核对，可补充学校或课程信息。")
        elif payment.accepts_credit_card == 0:
            # The school's channel forbids card tuition, so the tuition reward
            # scenario is unreachable — rank by overseas daily spend instead.
            portal = f"（详见{payment.portal_name}）" if payment.portal_name else ""
            steps.append(f"{ctx.school_name} 官方缴费说明：学费支付不接受信用卡/借记卡{portal}。"
                         "学费本身建议走学校指定通道或汇款办理；卡片更适合境外日常消费。")
            scenarios = [item for item in scenarios if item != "tuition"]
            limitations.append("已按学校官方缴费说明将学费场景调整为境外日常消费比较。")
        elif payment.accepts_credit_card == 1:
            steps.append(f"{ctx.school_name} 缴费通道可用信用卡支付学费（以缴费页面当日选项为准）。")
            channels = "、".join(payment.multi_currency_channels)
            if payment.supports_multi_currency == 1 and channels:
                steps.append(f"学校提供本地币/多币种缴费通道（{channels}）；"
                             "平台加点或汇率差未计入回赠估算。")
        else:
            channels = "、".join(payment.multi_currency_channels)
            if payment.supports_multi_currency == 1 and channels:
                steps.append(f"{ctx.school_name} 学费经本地币/多币种通道缴纳（{channels}）；"
                             "通道内是否可用卡支付以当日选项为准，卡片测算适用于通道内可卡支付或境外日常消费的情形。")
            else:
                steps.append(f"已核对 {ctx.school_name} 的官方缴费方式；"
                             "学费能否用信用卡缴纳未明确收录，请以学校缴费页面为准。")
        if payment is not None and payment.summary:
            # Only surface summaries already written in Chinese; the English
            # source quote (e.g. "HKUST research postgraduate tuition is an
            # annual Registry rate…") reads as a second school in a Chinese
            # answer and stays in the payload for evidence instead.
            if any("\u4e00" <= ch <= "\u9fff" for ch in payment.summary):
                steps.append(payment.summary)
        # Foreign-currency tuition (GBP/USD/...) carries FX drag either way.
        if ctx.rates and (ctx.rates[0].currency or "").upper() not in {"HKD", "CNY", "RMB", ""}:
            if "overseas" not in scenarios:
                scenarios.append("overseas")
        return {
            "school": ctx.school_name,
            "scenarios": scenarios,
            "steps": [item for item in steps if item],
            "limitations": limitations,
            "payment": payment.as_dict() if payment else None,
        }

    async def _catalogs(self, region, scope=None):
        markets = (region,) if scope and scope.get("market") == region else \
            STUDY_MARKETS.get(region, (region,))
        loaded = []
        for market in markets:
            catalog = await self.repository.active_products(market, self.clock())
            if scope and (scope.get("networks") or scope.get("no_annual_fee")):
                catalog = catalog.model_copy(update={
                    "products": [product for product in catalog.products
                                 if _in_card_scope(product, scope)]})
            loaded.append((market, catalog))
        return loaded

    @staticmethod
    def _combined(catalogs):
        products = [product for _, catalog in catalogs for product in catalog.products]
        first = catalogs[0][1]
        return CardCatalogRead(
            read_id="+".join(catalog.read_id for _, catalog in catalogs),
            market=first.market,
            products=products,
            source_total=sum(catalog.source_total or 0 for _, catalog in catalogs),
            source_active=sum(catalog.source_active or 0 for _, catalog in catalogs),
            source_filtered=sum(catalog.source_filtered or 0 for _, catalog in catalogs),
        )

    @staticmethod
    def _study_annual_fee(product):
        return next((fee for fee in product.fees
                     if fee.fee_type == "annual_fee" and fee.amount is not None), None)

    @staticmethod
    def _study_annual_fee_label(product):
        fee = CreditCardWorkflow._study_annual_fee(product)
        if fee is None:
            return "请见官网"
        return "免年费" if fee.amount == 0 else _money(fee.amount, fee.currency)

    @staticmethod
    def _study_fee_pair(products, facts, *, require_eligible=True):
        """Lower-fee vs higher-tier World Elite pair among comparable local cards."""
        assessed = [(assess_eligibility(product, facts), product) for product in products]
        available = [(assessment, product) for assessment, product in assessed
                     if assessment.status != "ineligible"]
        if len(available) < 2:
            return None
        best_status = min(_STATUS_ORDER[item.status] for item, _ in available)
        if require_eligible and best_status != _STATUS_ORDER["eligible"]:
            return None
        if not require_eligible and best_status > _STATUS_ORDER["unknown"]:
            return None
        peers = [product for assessment, product in available
                 if _STATUS_ORDER[assessment.status] == best_status]
        priced = [(product, CreditCardWorkflow._study_annual_fee(product)) for product in peers]
        priced = [(product, fee) for product, fee in priced if fee is not None]
        if len(priced) < 2:
            return None
        lower, lower_fee = min(priced, key=lambda item: (item[1].amount, item[0].card_id))
        premium = [(product, fee) for product, fee in priced
                   if product.card_tier == "World Elite" and fee.amount > lower_fee.amount]
        if not premium:
            return None
        return lower, max(premium, key=lambda item: (item[1].amount, item[0].card_id))[0]

    @staticmethod
    def _study_decision_pair(products, facts):
        """Ask a preference only when it can switch the preferred local card."""
        return CreditCardWorkflow._study_fee_pair(products, facts, require_eligible=True)

    @staticmethod
    def _all_ineligible_result(task, catalog, facts, extracted, basis):
        assessments = [assess_eligibility(product, facts) for product in catalog.products]
        if not assessments or not all(item.status == "ineligible" for item in assessments):
            return None
        excluded = {
            product.card_id: assessment.reasons
            for product, assessment in zip(catalog.products, assessments)
        }
        steps = [
            f"{product.product_name}：" + "；".join(
                _public_reason(reason).replace("不满足：", "申请条件：", 1)
                for reason in excluded[product.card_id] if reason.startswith("不满足："))
            for product in catalog.products
        ]
        return DomainResult(
            task_id=task.task_id, domain="credit_card", status="answered",
            answer=AnswerBody(
                summary="根据目前已确认的申请条件，暂时没有可推荐的汇丰信用卡。"
                        "若之后情况有变化，我们可以再为您比较适合的选择。",
                steps=steps),
            payload={"recommendations": CreditCardRecommendationSet(
                excluded=excluded).model_dump(mode="json"),
                     "catalog": CreditCardWorkflow._catalog_summary(catalog)},
            knowledge_read_id=catalog.read_id, observed_facts=extracted, observed_basis=basis,
        )

    def _study_answer(self, task, catalogs, facts, scenarios, extracted, basis,
                      tuition_check: dict | None = None):
        """Local HSBC shelf first; Hong Kong only as a low-approval backup."""
        steps, evidence, limitations = [], [], []
        if tuition_check:
            # The school's payment-channel reality leads the answer so a
            # forbidden channel is never buried under card recommendations.
            steps.extend(tuition_check["steps"])
        groups = []
        local_products = []
        local_shown = []
        decision_pair = None
        region = facts["application_region"]
        hk_resident = facts.get("hk_resident")
        for market, catalog in catalogs:
            if market == "CN-HK":
                continue
            local_products = catalog.products
            decision_pair = self._study_decision_pair(catalog.products, facts)
            lines, local_shown = self._local_lines(
                catalog.products, facts, scenarios, facts.get("reward_preference", "points"), market)
            us_account_path = facts.get("us_hsbc_account_path")
            # plan_to_open / not_applicable：仍展示美国卡供比较，不因尚未开户而只剩港卡。
            planned_us = market == "US" and us_account_path in {
                "plan_to_open", "not_applicable",
            }
            preview = planned_us or bool(local_shown and all(
                item["eligibility"] == "unknown" for item in local_shown))
            if local_shown:
                if us_account_path == "plan_to_open":
                    steps.append(
                        "提前了解：您计划先开立美国汇丰卓越理财支票账户，"
                        "下面先比较这几张卡的年费与权益。开户后仍须符合申请条件并通过银行审批。")
                elif us_account_path == "not_applicable":
                    steps.append(
                        "提前了解：您目前没有美国汇丰卓越理财支票账户；"
                        "下面仍先比较当地卡的年费与权益，正式申请通常需先开立该账户。")
                elif preview:
                    steps.append(
                        "提前了解：以下卡片可先比较权益与年费；"
                        "申请资格仍需核对当地账户、年龄及居住地等条件。")
                elif decision_pair and facts.get("study_card_focus") == "compare":
                    lower, premium = decision_pair
                    steps.append(
                        f"对比参考：{lower.product_name}年费"
                        f"{self._study_annual_fee_label(lower)}；"
                        f"{premium.product_name}年费"
                        f"{self._study_annual_fee_label(premium)}，更侧重旅行权益。"
                        "下面列出主要权益，方便您按实际使用情况选择。")
                steps.extend((GROUP_TITLES[market], PREREQUISITES[market]))
                steps.extend(lines)
            groups.append({"market": catalog.market,
                           "role": "preview" if preview and local_shown else
                                   "primary" if local_shown else "none",
                           "cards": local_shown})
            evidence.extend(product.official_url for product in catalog.products
                            if product.official_url)
        if region == "US":
            evidence.append(US_CARDS_URL)

        hk_catalog = next((item for market, item in catalogs if market == "CN-HK"), None)
        hk_products = hk_catalog.products if hk_catalog else []
        hk_shown = []
        if hk_products:
            steps.append(HK_BACKUP_TITLE)
            steps.append(
                "香港卡仅作备用参考。"
                + ("您是香港居民时，仍须满足各卡公开门槛。"
                   if hk_resident is True else
                   "您不是香港居民（或尚未确认）时，汇丰香港成文条件要求申请人一般为"
                   "香港居民，获批几率很低，最终由银行个案审批。")
            )
            steps.append(PREREQUISITES["CN-HK"])
            backup_facts = {**facts, "repayment_mode": facts.get("repayment_mode") or "full"}
            if hk_resident is not True:
                backup_facts = {**backup_facts, "hk_resident": False}
            backup_lines, hk_shown = self._hk_backup_lines(hk_products, backup_facts)
            steps.extend(backup_lines)
            groups.append({"market": "Hong Kong", "role": "backup", "cards": hk_shown})
            evidence.extend(product.official_url for product in hk_products
                            if product.official_url)

        eligible_local = next(
            (item["product_name"] for item in local_shown
             if item.get("eligibility") == "eligible"),
            None,
        )
        pending_local = next(
            (item["product_name"] for item in local_shown
             if item["eligibility"] == "unknown" and item.get("pending_prerequisites")), None)
        place = {"UK": "英国", "US": "美国"}.get(region, region)
        if (region == "US" and facts.get("us_hsbc_account_path") in {
                "plan_to_open", "not_applicable"} and local_shown):
            names = "、".join(item["product_name"] for item in local_shown)
            if facts.get("us_hsbc_account_path") == "plan_to_open":
                summary = (
                    f"您计划先开立美国汇丰卓越理财支票账户，我们先为您介绍{names}，"
                    "方便提前比较年费与权益。正式申请仍需在开户后核对条件并通过银行审批。"
                    + ("另附香港卡作备用（获批可能较难）。" if hk_shown else "")
                )
            else:
                summary = (
                    f"您目前没有美国汇丰卓越理财支票账户；我们仍先介绍{names}，"
                    "方便比较年费与权益。正式申请通常需先开立该账户并通过银行审批。"
                    + ("另附香港卡作备用（获批可能较难）。" if hk_shown else "")
                )
        elif local_shown and facts.get("study_card_focus") == "compare" and decision_pair:
            summary = (
                "这两张当地卡的年费与旅行权益各有取舍。先为您并列介绍，"
                "您可以结合实际使用频率决定哪张更合适。"
                + ("香港卡仅作备用。" if hk_shown else "")
            )
        elif eligible_local:
            focus = facts.get("study_card_focus")
            reason = ("您更重视年费成本。" if focus == "lower_fee" else
                      "您更重视旅行权益，也接受较高年费。" if focus == "travel_benefits" else "")
            summary = (
                f"留学{place}优先考虑当地汇丰卡，主推 {eligible_local}。"
                f"{reason}"
                + ("香港卡仅作备用，获批可能较难。" if hk_shown else "")
            )
        elif pending_local:
            summary = (
                f"留学{place}可先了解 {pending_local}；请先完成当地账户等申请前提，再核对资格。"
                + ("另附香港卡作备用（获批可能较难）。" if hk_shown else "")
            )
        elif local_shown:
            summary = (
                "这些当地汇丰卡可先了解和比较；申请资格仍需核对当地账户、"
                "年龄及居住地等条件。"
                + ("另附香港卡作备用（获批可能较难）。" if hk_shown else "")
            )
        elif (not local_shown and local_products
              and (facts.get("applicant_age_band") == "under_18"
                   or (facts.get("applicant_age") is not None
                       and facts["applicant_age"] < 18))):
            summary = (
                "所选地区的汇丰信用卡设有最低申请年龄。"
                "等符合年龄条件后，我们可以再为您比较卡片权益与年费。"
            )
        elif region == "US" and local_products:
            if facts.get("us_hsbc_account_path") == "plan_to_open":
                summary = (
                    "汇丰美国现有信用卡目录暂未列出学生卡。两款卡的申请前提"
                    "是已持有美国汇丰卓越理财支票账户；待账户开立后，我们可以再结合"
                    "您的需求比较两张卡的权益与年费。"
                    + ("另附香港卡作备用（获批可能较难）。" if hk_shown else "")
                )
            else:
                summary = (
                    "汇丰美国现有信用卡目录暂未列出学生卡；两款卡均要求"
                    "已持有美国汇丰卓越理财支票账户。"
                    + ("下列香港卡仅作备用参考，获批可能较难。" if hk_shown else
                       "基于这些产品条件，这次先不列出具体卡片。"
                       "如果之后希望重新了解美国当地的信用卡选择，"
                       "我们可以随时再为您比较。")
                )
        elif local_products:
            summary = (
                "目前没有确认适合您申请的汇丰英国信用卡。请核对年龄、英国居民身份及当地账户条件。"
                + ("另附香港卡作备用（获批可能较难）。" if hk_shown else "")
            )
        elif hk_shown:
            summary = "当地汇丰卡资料暂缺；下列香港卡仅作备用参考，获批可能较难。"
        else:
            summary = "所选地区的汇丰信用卡资料暂缺。"

        limitations.append(
            "优先比较汇丰当地发行的卡；香港卡只作备用，港币费用不与美元或英镑混排。")
        if hk_products and hk_resident is not True:
            limitations.insert(0, (
                "备用香港卡：非香港居民获批几率很低，不代表现在可以提交或获批，"
                "最终由银行审批。"
            ))
        if local_shown:
            limitations.append("申请仍须满足当地条件并通过银行审批。")
            limitations.append("迎新礼遇未计入年度净值，申请前请以官网最新条款为准。")
        if tuition_check:
            limitations.extend(tuition_check["limitations"])

        now = self.clock()
        ready_products = local_products or hk_products
        product_data_ready = bool(ready_products) and all(
            product.official_url
            and product.last_verified_at
            and now - product.last_verified_at <= timedelta(days=30)
            and all(item.source_url and item.last_verified_at
                    for item in [*product.reward_rules, *product.fees, *product.benefits])
            for product in ready_products
        )
        if not product_data_ready and (local_shown or hk_shown):
            limitations.insert(0, "部分年费或奖赏资料仍待官网核验，请以发卡机构页面为准。")
        return DomainResult(
            task_id=task.task_id, domain="credit_card",
            status="answered" if product_data_ready else "needs_verification",
            answer=AnswerBody(summary=summary, steps=steps,
                              limitations=list(dict.fromkeys(limitations))),
            evidence_refs=list(dict.fromkeys(evidence)),
            next_actions=["申请前在发卡机构官网确认资格、年费、奖励上限和最新条款。"],
            notices=[] if product_data_ready else [Notice(
                code="card_product_fields_need_verification",
                message="信用卡奖励和年费字段尚未逐项完成官方核验")],
            payload={"groups": groups, "market": region,
                     "tuition_payment_check": (
                         {key: tuition_check[key] for key in ("school", "payment", "scenarios")}
                         if tuition_check else None)},
            knowledge_read_id="+".join(catalog.read_id for _, catalog in catalogs),
            observed_facts=extracted, observed_basis=basis,
        )

    def _hk_backup_lines(self, products, facts):
        """List Hong Kong cards as backup without requiring a full HK interview."""
        free, paid = [], []
        for product in products:
            annual = next((fee for fee in product.fees if fee.fee_type == "annual_fee"), None)
            (free if annual is not None and annual.amount == 0 else paid).append(product)
        ordered = free + paid
        lines, shown = [], []
        for product in ordered[:5]:
            assessment = assess_eligibility(product, facts)
            if facts.get("hk_resident") is not True:
                assessment = assessment.model_copy(update={
                    "low_approval_chance": True,
                    "reasons": list(dict.fromkeys(
                        [*assessment.reasons, "非香港居民：备用港卡，获批几率很低"])),
                })
            card_lines = self._plain_card_lines(product, assessment, facts)
            if assessment.low_approval_chance:
                card_lines = [
                    ("申请限制：备用港卡，非香港居民获批几率很低，需银行个案审批"
                     if line.startswith("申请限制：") else line)
                    for line in card_lines
                ]
                if not any(line.startswith("申请限制：") for line in card_lines):
                    card_lines.append(
                        "申请限制：备用港卡，非香港居民获批几率很低，需银行个案审批")
            lines.extend(card_lines)
            shown.append({
                "card_id": product.card_id,
                "product_name": product.product_name,
                "eligibility": assessment.status,
                "role": "backup",
                "reasons": assessment.reasons,
            })
        return lines, shown

    @staticmethod
    def _local_lines(products, facts, scenarios, preference, market):
        """Rank one local-market shelf without comparing values across currencies.

        Eligibility remains the primary ordering key. A stated fee-versus-travel
        choice selects the primary card when it can genuinely change the result;
        otherwise supplied spending scenarios are only a secondary ordering hint.
        """
        scenario_weights = {
            scenario: len(scenarios) - index
            for index, scenario in enumerate(scenarios)
        }

        def fit(product):
            score = 0
            if market == "UK":
                premier = facts.get("uk_hsbc_account_path") == "premier_account"
                if (premier and product.card_tier in {"Premier", "World Elite"}
                        or not premier and product.card_tier == "Student"):
                    score += 100
            for scenario, weight in scenario_weights.items():
                categories = SCENARIO_RULES.get(scenario, {scenario})
                if any(rule.spend_category in categories for rule in product.reward_rules):
                    score += weight
            if preference != "no_preference" and any(
                    rule.reward_type == preference for rule in product.reward_rules):
                score += len(scenarios) + 1
            return score

        def annual_fee(product):
            fee = next((item for item in product.fees
                        if item.fee_type == "annual_fee"), None)
            return fee.amount if fee and fee.amount is not None else Decimal("Infinity")

        planned_account = (
            (market == "US" and facts.get("us_hsbc_account_path") in {
                "plan_to_open", "not_applicable"})
            or (market == "UK" and facts.get("uk_hsbc_account_path") == "plan_to_open")
        )
        pair = CreditCardWorkflow._study_decision_pair(products, facts)
        # plan_to_open / not_applicable 时卡片多为 pending，仍按年费排出首选/备选。
        if not pair and planned_account:
            pair = CreditCardWorkflow._study_fee_pair(
                products, facts, require_eligible=False)
        focus = facts.get("study_card_focus")
        if planned_account and focus not in {"lower_fee", "travel_benefits", "compare"}:
            focus = "lower_fee"
        us_path = facts.get("us_hsbc_account_path") if market == "US" else None
        us_ok = market != "US" or us_path in {
            "existing_premier_checking", "plan_to_open", "not_applicable",
        }
        decided = bool(pair and focus in {"lower_fee", "travel_benefits"} and us_ok)
        preferred_id = (pair[0 if focus == "lower_fee" else 1].card_id
                        if decided else None)

        ranked = sorted(
            ((assess_eligibility(product, facts), product) for product in products),
            key=lambda item: (
                _STATUS_ORDER.get(item[0].status, 9),
                0 if item[1].card_id == preferred_id else 1 if decided else 0,
                -fit(item[1]),
                annual_fee(item[1]),
                item[1].product_name,
            ),
        )
        lines, shown = [], []
        for assessment, product in ranked:
            if assessment.status == "ineligible":
                continue
            card_lines = CreditCardWorkflow._plain_card_lines(product, assessment, facts)
            role = "primary" if product.card_id == preferred_id else "alternative"
            if decided:
                lines.append(f"{'主推荐' if role == 'primary' else '其他选择'}：{product.product_name}")
                if role == "primary":
                    reason = (
                        "您希望控制年费；这张卡在当前两款中年费较低，"
                        "适合先关注持卡成本与日常使用。"
                        if focus == "lower_fee" else
                        "您更看重旅行权益；这张卡提供相应礼遇，年费也较高，"
                        "可结合预计使用频率衡量。"
                    )
                    lines.append(f"为什么适合您：{reason}")
                else:
                    alternative = (
                        "如果之后更常使用旅行权益，也可以对比这张年费较高的卡。"
                        if product.card_id == pair[1].card_id else
                        "如果更希望控制年费，可以考虑这张持卡成本较低的卡。"
                    )
                    lines.append(f"推荐理由：{alternative}")
                lines.extend(card_lines[1:])
            else:
                lines.extend(card_lines)
            item = {
                "card_id": product.card_id,
                "product_name": product.product_name,
                "eligibility": assessment.status,
                "reasons": [_public_reason(reason) for reason in assessment.reasons],
                "missing_fields": assessment.missing_fields,
                "pending_prerequisites": [_public_reason(reason)
                                          for reason in assessment.pending_prerequisites],
            }
            if decided:
                item["role"] = role
            shown.append(item)
        return lines, shown

    @staticmethod
    def _fee_text_from_product(product):
        annual = next((fee for fee in product.fees if fee.fee_type == "annual_fee"), None)
        if annual is None or annual.amount is None:
            text = "请见官网"
        elif annual.amount == 0:
            text = "免年费"
        else:
            text = _money(annual.amount, annual.currency)
            if annual.waiver_condition:
                text += f"（{_waiver_text(annual.waiver_condition)}）"
        fx = next((fee for fee in product.fees if fee.fee_type == "foreign_transaction_fee"), None)
        if fx is not None and fx.rate is not None:
            text += f"；外币手续费 {(fx.rate * 100).normalize():f}%"
        return text

    @staticmethod
    def _perks_from_product(product):
        names = [_benefit_text(benefit.benefit_name) for benefit in product.benefits
                 if benefit.benefit_name]
        if names:
            return "、".join(names[:5])
        return "详见官网奖赏与附加权益"

    @staticmethod
    def _restriction_text(assessment):
        if assessment is None:
            return None
        if assessment.low_approval_chance:
            return "非香港居民，获批几率很低，需银行个案审批"
        if assessment.status == "needs_bank_assessment":
            return "收入未达公开门槛时，可提交资产证明，由银行个案审批"
        if assessment.status == "ineligible":
            return "目前不符合该卡公开申请条件"
        if assessment.status == "unknown":
            return "申请时须满足官网列明的年龄、居住地及当地汇丰账户条件"
        return None

    @staticmethod
    def _plain_card_lines(product, assessment, facts=None):
        network = NETWORK_ZH.get(product.card_network, "银行卡")
        lines = [
            f"{product.product_name}（{network}）",
            f"年费：{CreditCardWorkflow._fee_text_from_product(product)}",
            f"主要权益：{CreditCardWorkflow._perks_from_product(product)}",
        ]
        facts = facts or {}
        underage = facts.get("applicant_age_band") == "under_18" or (
            facts.get("applicant_age") is not None and facts["applicant_age"] < 18)
        if assessment.status == "ineligible" and underage:
            restriction = "目前未达到该卡的年龄门槛"
        elif assessment.status == "ineligible" and product.market in {"UK", "US"}:
            restriction = "目前尚未持有所需的当地汇丰账户"
        elif assessment.pending_prerequisites:
            if product.market == "UK" and any(
                    rule.value_text == "uk_student_account"
                    for rule in product.eligibility_rules) and facts.get(
                    "uk_hsbc_account_path") == "plan_to_open":
                restriction = "先开立汇丰英国学生账户，并符合年龄及英国居民条件；之后由银行审批"
            elif product.market == "US":
                restriction = "先开立美国汇丰卓越理财支票账户；之后由银行审批"
            elif product.market == "UK":
                restriction = "先满足英国居民条件，并核对年龄及当地汇丰账户资格；之后由银行审批"
            else:
                restriction = "先完成当地账户等申请前提；之后由银行审批"
        elif assessment.status == "unknown" and product.market == "UK":
            restriction = "还需核对英国居民身份、年龄及当地汇丰账户条件"
        elif assessment.status == "unknown" and product.market == "US":
            restriction = "需核对是否已有美国汇丰卓越理财支票账户及年龄条件"
        else:
            restriction = CreditCardWorkflow._restriction_text(assessment)
        if restriction:
            label = ("申请前提" if assessment.pending_prerequisites
                     and assessment.status != "ineligible" else "申请限制")
            lines.append(f"{label}：{restriction}")
        return lines

    @staticmethod
    def _format_card_lines(card, *, show_estimates=True, provisional=False, near_tie=False,
                           student_card_confirmed=False):
        """Format a concise, customer-facing recommendation rather than raw DB fields."""
        label = {"primary": "主推荐", "supplement": "场景搭配", "alternative": "其他选择"}[card.role]
        if provisional and card.role == "primary":
            label = "初步选择"
        elif card.role == "alternative" and card.eligibility.status == "unknown":
            label = "待核对选择"
        elif near_tie:
            label = "附加推荐"
        if card.annual_fee is None:
            fee = "请见官网"
        elif card.annual_fee == 0:
            fee = "免年费"
            waiver = _waiver_text(card.annual_fee_waiver) if card.annual_fee_waiver else ""
            if waiver and waiver != fee:
                fee += f"（{waiver}）"
        else:
            fee = _money(card.annual_fee, card.annual_fee_currency)
            if card.annual_fee_waiver:
                fee += f"（{_waiver_text(card.annual_fee_waiver)}）"
            if (card.ongoing_annual_fee_hkd is not None and card.ongoing_annual_fee_hkd > 0
                    and card.estimated_annual_fee_hkd == 0):
                fee += f"；豁免期后年费每年{card.ongoing_annual_fee_hkd:,.0f}港元"
        perks = [_benefit_text(item) for item in card.main_benefits[:3]]
        scenes = [_category_text(s) for s in card.matched_scenarios if s]
        miles = card.reward_units.get("miles") if show_estimates else None
        points = card.reward_units.get("points") if show_estimates else None
        if miles is not None and miles > 0:
            perks.append(f"约 {miles:,.0f} 飞行里程/年")
        elif points is not None and points > 0:
            perks.append(f"约 {points:,.0f} 积分/年")
        perk_text = "；".join(perks) if perks else "详见官网奖赏与附加权益"
        scene_text = "、".join(scenes)
        if card.role == "alternative" and card.eligibility.status == "unknown":
            opening = "这张卡也值得比较；部分申请条件尚未核实，申请前请按官网要求确认。"
        elif near_tie:
            opening = "这张卡和主推荐在您关注的场景下表现接近，值得一起比较年费、奖赏上限和申请条件。"
        elif card.role == "primary" and set(card.eligibility.missing_fields) & {
                "full_time_student", "student_institution_eligible"}:
            opening = ("从您的留学画像看，学生卡值得先了解。全日制身份及学校是否在指定名单内"
                       "尚未核实，所以这只是有条件的初步选择；申请前请按汇丰要求核对。")
        elif card.role == "primary" and (card.eligibility.matched_path == "student"
                                        or student_card_confirmed):
            opening = ("您已确认全日制学生身份和指定院校资格，这张卡有独立的学生申请路径，"
                       "值得先核对申请条件；最终仍由银行审批。")
        elif card.role == "supplement" and scene_text:
            opening = f"可以和主卡搭配使用，专门补足您在{scene_text}方面的需求。"
        elif scene_text:
            opening = f"如果您希望把{scene_text}用得更划算，这张卡会是更贴合的选择。"
        else:
            opening = ("根据目前提供的资料，这张卡值得先看看。" if provisional else
                       "综合您的申请条件和消费习惯，这张卡的整体价值更突出。")

        value_points = []
        if (show_estimates and card.estimated_annual_reward_hkd is not None
                and card.estimated_annual_reward_hkd > 0):
            value_points.append(
                f"按您填写的消费区间估算，每年约可获得 "
                f"{card.estimated_annual_reward_hkd:,.0f}港元奖赏回报")
        if perks:
            value_points.append("还可享" + "、".join(perks[:2]))
        if card.estimated_annual_fee_hkd == 0:
            if card.annual_fee_waiver:
                value_points.append(_waiver_text(card.annual_fee_waiver))
            else:
                value_points.append("无需支付年费")
        reason_text = opening
        if value_points:
            reason_text += " " + "；".join(value_points) + "，日常使用更轻松。"
        lines = [
            f"{label}：{card.product_name}",
            f"为什么适合您：{reason_text}",
            f"年费：{fee}",
            f"主要权益：{perk_text}",
        ]
        restriction = CreditCardWorkflow._restriction_text(card.eligibility)
        if restriction:
            lines.append(f"申请限制：{restriction}")
        return lines

    @staticmethod
    def _catalog_summary(catalog):
        return {
            "market": catalog.market,
            "returned_products": len(catalog.products),
            "source_total": catalog.source_total,
            "source_active": catalog.source_active,
            "source_filtered": catalog.source_filtered,
        }

    @staticmethod
    def _hk_include_near_ties(result):
        """Show close HK alternatives alongside the main card, at most three total."""
        primary = result.primary
        supplements = list(result.supplements)
        selected = {primary.card_id, *(card.card_id for card in supplements)}
        near_tie_ids = set()
        tolerance = max(Decimal("50"), abs(primary.score) * Decimal("0.10"))
        for card in result.ranked_candidates:
            if len(supplements) >= 2:
                break
            if card.card_id in selected:
                continue
            if card.eligibility.status != primary.eligibility.status:
                continue
            if (primary.eligibility.matched_path != "asset_review"
                    and card.eligibility.matched_path == "asset_review"):
                continue
            if set(card.eligibility.missing_fields) != set(primary.eligibility.missing_fields):
                continue
            if abs(primary.score - card.score) > tolerance:
                continue
            supplements.append(card.model_copy(update={"role": "supplement"}))
            selected.add(card.card_id)
            near_tie_ids.add(card.card_id)
        return result.model_copy(update={
            "supplements": supplements,
            "alternatives": [card for card in result.alternatives if card.card_id not in selected],
        }), near_tie_ids

    @staticmethod
    def _derive_hk_student_facts(facts: dict) -> None:
        """Settle HK student-card facts the Profile already answers, in place.

        Every Hong Kong school the product catalogs is on HSBC's designated
        institution list (data policy, user-confirmed), so a bound Hong Kong
        school settles student_institution_eligible; an explicit student
        identity (role or applicant) at a Hong Kong school settles
        full_time_student. Only None values are filled — explicit answers,
        including explicit negatives, are never overridden. The derived
        values stay out of the observed facts on purpose: they are
        inference, not something the customer stated this turn.
        """
        school = str(facts.get("school") or "").strip()
        in_hk = (facts.get("application_region") in {"CN-HK", "HK"}
                 or facts.get("destination_country") in {"CN-HK", "HK", "Hong Kong",
                                                          "香港", "中国香港"})
        school_is_hk = bool(school) and in_hk
        if school_is_hk and facts.get("student_institution_eligible") is None:
            facts["student_institution_eligible"] = True
        is_student = (facts.get("applicant_identity") == "student"
                      or facts.get("role") == "student")
        is_parent = facts.get("applicant_identity") == "parent"
        if (is_student and not is_parent and school_is_hk
                and facts.get("full_time_student") is None):
            facts["full_time_student"] = True

    def _hk_best_question(self, products, facts, scope):
        """Ask only when an answer can change the HK shelf or leading card.

        The simulations use the same eligibility and ranking rules as the
        final answer. Uniform option weights are a heuristic, not a claim
        about real customer probabilities. Sensitive questions pay a penalty.
        """
        asked = set(scope.get("asked_fields") or [])
        remaining = [product for product in products
                     if assess_eligibility(product, facts).status != "ineligible"]
        if not remaining:
            return None

        options = dict(QUESTION_OPTIONS)
        options["annual_income_band_hkd"] = _income_options(remaining)
        prompts = {
            "applicant_age_band": "为了核对申请年龄，方便了解您是否已满 18 岁吗？",
            "annual_income_band_hkd": "有几张卡的申请门槛不同。方便了解您的税前年收入大致区间吗？选范围即可。",
            "spending_scenarios": "想先了解您最常在哪些地方用卡？选一两个常用场景就好。",
            "monthly_card_spend_band_hkd": "如果方便，您每月刷卡大约在什么范围？这只用于估算回赠与年费。",
            "reward_preference": "您更喜欢现金回赠、积分还是里程？没有偏好也可以。",
            "hsbc_premier_path": "如果您有汇丰卓越理财关系，部分选择会不同。您属于哪种情况？",
            "asset_band": "如果收入条件不适用，您愿意考虑由银行参考资产证明作个案审批吗？",
            "mainland_payment_method": "您提到内地消费，平时更常用手机扫码还是实体卡？这会影响实际回赠。",
            "hk_resident": "方便了解您是否为香港居民吗？这会影响香港信用卡的申请机会，最终仍由银行审批。",
            "hsbc_relationship_balance_band": "如果考虑卓越理财信用卡，方便了解您在汇丰的总关系结余大致区间吗？选范围即可。",
        }
        # Age and income establish eligibility. Allow at most three additional
        # ranking questions so these basics do not use the entire preference
        # budget; a selected card may still need a decisive application fact.
        for field, known, aliases in (
                ("applicant_age_band", facts.get("applicant_age") is not None
                 or facts.get("applicant_age_band") is not None,
                 {"applicant_age", "applicant_age_band"}),
                ("annual_income_band_hkd", facts.get("annual_income_hkd") is not None
                 or facts.get("annual_income_band_hkd") is not None,
                 {"annual_income_hkd", "annual_income_band_hkd"})):
            if field not in asked and not known and any(
                    aliases.intersection(assess_eligibility(product, facts).missing_fields)
                                 for product in remaining):
                choices = options.get(field) or []
                if len(choices) >= 2:
                    return field, prompts[field], choices
        # Residency is the written basic requirement of every HK card on the
        # shelf; without it every card stays "unknown / to-verify" and the
        # answer degrades into a disclaimer list. Ask it with the basics.
        if ("hk_resident" not in asked and facts.get("hk_resident") is None
                and any(rule.required and rule.rule_type == "residency"
                        for product in remaining for rule in product.eligibility_rules)):
            choices = options.get("hk_resident") or []
            if len(choices) >= 2:
                return "hk_resident", prompts["hk_resident"], choices
        # Consumption scenarios shape the ranking copy ("如果您希望把…用得更
        # 划算") and the coverage gaps, so ask once even when the simulated
        # shortlist happens to look stable under every option.
        if ("spending_scenarios" not in asked
                and not facts.get("spending_scenarios")
                and len(options.get("spending_scenarios") or []) >= 2):
            return ("spending_scenarios", prompts["spending_scenarios"],
                    options["spending_scenarios"])
        # Without the monthly spend band the answer cannot show reward
        # estimates at all ("未提供每月消费额，本次不展示回赠金额估算"),
        # so ask it once instead of disclaiming it away.
        if ("monthly_card_spend_band_hkd" not in asked
                and not facts.get("monthly_card_spend_band_hkd")
                and len(options.get("monthly_card_spend_band_hkd") or []) >= 2):
            return ("monthly_card_spend_band_hkd", prompts["monthly_card_spend_band_hkd"],
                    options["monthly_card_spend_band_hkd"])
        ranking_fields = {"spending_scenarios", "monthly_card_spend_band_hkd",
                          "reward_preference", "mainland_payment_method",
                          "hsbc_premier_path", "asset_band",
                          "hsbc_relationship_balance_band"}
        if len(remaining) == 1 or len(asked & ranking_fields) >= 3:
            scenarios = facts.get("spending_scenarios") or []
            if isinstance(scenarios, str):
                scenarios = [item for item in scenarios.split(",") if item]
            selected = recommend(remaining, facts, scenarios,
                                 facts.get("reward_preference") or "no_preference",
                                 now=self.clock()).primary
            missing = set(selected.eligibility.missing_fields) if selected else set()
            critical = (
                ("hsbc_premier_path", {"hsbc_premier_path", "premier_relationship"}),
                ("hsbc_relationship_balance_band", {"hsbc_relationship_balance_band",
                                                    "hsbc_total_relationship_balance_hkd"}),
                ("asset_band", {"asset_proof_available", "provable_financial_assets_hkd",
                                "asset_band"}),
                ("hk_resident", {"hk_resident"}),
            )
            for field, aliases in critical:
                if (field not in asked and missing.intersection(aliases)
                        and facts.get(field) is None):
                    choices = options.get(field) or []
                    if len(choices) >= 2:
                        return field, prompts[field], choices
            return None
        fields = []
        if facts.get("applicant_age") is None and facts.get("applicant_age_band") is None:
            fields.append("applicant_age_band")
        if facts.get("annual_income_hkd") is None and facts.get("annual_income_band_hkd") is None:
            fields.append("annual_income_band_hkd")
        if (facts.get("hsbc_premier_path") is None and facts.get("premier_relationship") is None
                and any(rule.required and rule.rule_type == "banking_relationship"
                        for product in remaining for rule in product.eligibility_rules)):
            fields.append("hsbc_premier_path")
        if any(needs_asset_information(product, facts) for product in remaining):
            fields.append("asset_band")
        if not facts.get("spending_scenarios"):
            fields.append("spending_scenarios")
        elif "mainland_china" in facts["spending_scenarios"] and not facts.get("mainland_payment_method"):
            fields.append("mainland_payment_method")
        if not facts.get("monthly_card_spend_band_hkd"):
            fields.append("monthly_card_spend_band_hkd")
        if not facts.get("reward_preference"):
            fields.append("reward_preference")

        ranked = []
        for field in dict.fromkeys(fields):
            if field in asked:
                continue
            choices = options.get(field) or []
            if len(choices) < 2:
                continue
            outcomes = []
            for choice in choices:
                value = choice["value"]
                simulated = {**facts, field: [value] if field == "spending_scenarios" else value}
                scenarios = simulated.get("spending_scenarios") or []
                if isinstance(scenarios, str):
                    scenarios = [item for item in scenarios.split(",") if item]
                scored = recommend(products, simulated, scenarios,
                                   simulated.get("reward_preference") or "no_preference", now=self.clock())
                viable = frozenset(product.card_id for product in products
                                   if assess_eligibility(product, simulated).status != "ineligible")
                if scored.primary is None:
                    displayed = ()
                else:
                    # The customer sees a shortlist, not only the first card.
                    # Usage and reward answers can change its other members
                    # even when a conditional student card remains first.
                    extras = [*scored.supplements, *scored.conditional_alternatives]
                    if not scored.conditional_alternatives:
                        extras.extend(scored.alternatives)
                    displayed = (scored.primary.card_id,
                                 *(card.card_id for card in extras[:2]))
                outcomes.append((displayed, viable))
            shortlist_counts = Counter(shortlist for shortlist, _ in outcomes)
            shortlist_entropy = -sum((count / len(outcomes)) * log2(count / len(outcomes))
                                     for count in shortlist_counts.values())
            elimination_gain = sum(
                2 * min(sum(product.card_id in viable for _, viable in outcomes) / len(outcomes),
                        sum(product.card_id not in viable for _, viable in outcomes) / len(outcomes))
                for product in remaining) / len(remaining)
            burden = {"annual_income_band_hkd": .3, "asset_band": .7,
                      "hsbc_premier_path": .3, "monthly_card_spend_band_hkd": .2}.get(field, 0)
            # Normalize by option count so a seven-choice lifestyle question
            # does not win merely because it has more possible answers.
            gain = 3 * shortlist_entropy / log2(len(choices)) + 2 * elimination_gain - burden
            if gain > .5:
                ranked.append((gain, field, prompts[field], choices))
        if not ranked:
            return None
        _, field, prompt, choices = max(ranked, key=lambda item: item[0])
        return field, prompt, choices

    @staticmethod
    def _format_borrowing_costs(product):
        by_type = {}
        for term in product.borrowing_terms:
            by_type.setdefault(term.term_type, []).append(term)
        purchase = by_type["purchase_apr"][0]
        free = by_type["interest_free_period"][0]
        cash_examples = sorted((term for term in by_type.get("cash_instalment_example", [])
                                if term.available and term.apr is not None
                                and term.total_repayment is not None),
                               key=lambda item: item.tenor_months or 0)
        examples = "；".join(
            f"{term.tenor_months}期实际年利率{term.apr * 100:.2f}%、总还款{term.total_repayment:,.0f}港元"
            for term in cash_examples)
        spending = by_type["spending_instalment"][0]
        cash_offer = by_type["cash_instalment_offer"][0]
        instalment = (
            f"现金分期当前公布每月手续费低至 {cash_offer.monthly_fee_rate * 100:.2f}%；"
            f"十万港元官方示例：{examples}。消费分期手续费为个人报价，"
            "需在汇丰香港手机应用程序或热线确认。"
            if spending.available else "汇丰条款列明此卡不适用现金分期和消费分期。"
        )
        return (f"借款成本：消费实际年利率{purchase.apr * 100:.2f}%，最长免息期"
                f"{free.max_interest_free_days} 天。{instalment}")

    @staticmethod
    def _needs_input(task, request, extracted, basis, missing, prompts, options):
        selected = missing[:1]
        name = request.profile_name
        return DomainResult(
            task_id=task.task_id, domain="credit_card", status="needs_input",
            answer=AnswerBody(summary=_addressed(
                                  "我再了解一点您的情况，就能把选择缩小到更合适的卡。", name),
                              # Steps address the person typing: keep the polite 您.
                              steps=["您可以按方便的程度选择，不确定的地方我们也可以先保留。"]),
            missing_fields=missing,
            follow_up_questions=[_question(
                field, _addressed(prompts[field], name), request.profile_version, options=options[field],
                input_type="multi_select" if field == "spending_scenarios" else "single_select",
                max_selections=3 if field == "spending_scenarios" else None)
                                 for field in selected],
            observed_facts=extracted, observed_basis=basis,
        )

    @staticmethod
    def _repository_unavailable(task, extracted, basis):
        return DomainResult(
            task_id=task.task_id, domain="credit_card", status="needs_verification",
            answer=AnswerBody(summary="暂时无法读取信用卡产品库，请稍后重试。"),
            next_actions=["恢复数据库连接后重新运行信用卡推荐。"],
            notices=[Notice(code="credit_card_repository_unavailable", message="信用卡资料读取失败")],
            observed_facts=extracted, observed_basis=basis,
        )

    @staticmethod
    def _empty_catalog(task, catalog, extracted, basis, scope=None):
        scoped = bool(scope and (scope.get("networks") or scope.get("no_annual_fee")))
        return DomainResult(
            task_id=task.task_id, domain="credit_card", status="cannot_answer",
            answer=AnswerBody(summary="目前在您指定的范围内没有找到有效的信用卡产品。" if scoped
                              else "目前没有该地区有效的信用卡产品资料。"),
            next_actions=["您可以放宽卡组织或年费条件后再比较。"] if scoped else
                         ["接入该地区信用卡产品及门槛、奖励、费用和权益数据。"],
            knowledge_read_id=catalog.read_id, observed_facts=extracted, observed_basis=basis,
        )
