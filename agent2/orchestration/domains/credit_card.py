"""Credit-card recommendation workflow backed by the shared RDS product tables."""

import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from agent2 import clarify
from agent2.models import AnswerBody, FollowUpQuestion, Notice

from ..composer import is_necessity_question
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
NETWORK_ZH = {"Visa": "威士卡", "Mastercard": "万事达卡", "UnionPay": "银联卡"}
WAIVERS_ZH = {
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
        {"value": "not_applicable",
         "label": "目前没有美国汇丰卓越理财支票账户，想先了解其他选择"},
    ],
    "uk_hsbc_account_path": [
        {"value": "premier_account",
         "label": "是，已有英国汇丰卓越理财或私人银行账户"},
        {"value": "plan_to_open",
         "label": "否，优先看学生卡（需先开英国学生账户）"},
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
        r"怎么样|好不好|值得|适合我|适合谁|介绍|讲讲|评价|优缺点|"
        r"年费|费用|权益|回赠|返现|积分|里程|申请条件|能不能申请|区别|对比|哪个好",
        text, re.I))


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

        # Product Q&A is a chat capability, not another recommendation run.
        # It must answer “Red 卡怎么样 / 年费多少 / 和 Pulse 有什么区别”
        # without collecting every preference used by the ranking engine.
        capability_qa = any(
            action.operation in {"explain", "compare", "assess", "how_to", "troubleshoot"}
            for action in task.actions
        )
        if capability_qa or _looks_like_card_product_question(request.text):
            overview = await self._product_question(task, request, facts, extracted, basis)
            if overview is not None:
                return overview
        # General servicing knowledge is a credit-card-domain capability, not
        # a new router branch.  It is used only when no specific product answer
        # was available above, and it never starts a recommendation interview.
        general_knowledge = await self.knowledge.answer(task, request, facts, extracted, basis)
        if general_knowledge is not None:
            return general_knowledge

        # 「要不要办信用卡」：先确认意愿，再进入普通选卡流程。
        if facts.get("want_credit_card") is None and is_necessity_question(request.text):
            return DomainResult(
                task_id=task.task_id, domain="credit_card", status="needs_input",
                answer=AnswerBody(
                    summary="信用卡不是硬性必需。您这次想继续看看可申请的卡吗？",
                    steps=[
                        "若选择了解，我会按留学地优先推荐当地汇丰卡，香港卡仅作备用。",
                        "若这次先不办也没关系；之后直接说「推荐信用卡」即可。",
                    ],
                ),
                missing_fields=["want_credit_card"],
                follow_up_questions=[_question(
                    "want_credit_card",
                    "信用卡不是硬性必需。您这次想继续看看可申请的卡吗？",
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

        if facts.get("application_region") is None:
            missing = ["application_region"]
            if facts.get("applicant_age") is None and facts.get("applicant_age_band") is None:
                missing.append("applicant_age_band")
            prompts = {
                "application_region": (
                    "您准备去哪里留学？去英国或美国时，会优先推荐汇丰当地卡，"
                    "并另列香港卡作备用（非香港居民获批可能较难）。"
                ),
                "applicant_age_band": "您目前是否已满 18 岁？",
            }
            return self._needs_input(task, request, extracted, basis, missing, prompts, QUESTION_OPTIONS)

        if facts["application_region"] in STUDY_MARKETS:
            return await self._run_study(task, request, facts, extracted, basis)

        catalogs = None
        if facts.get("annual_income_hkd") is None and facts.get("annual_income_band_hkd") is None:
            try:
                catalogs = await self._catalogs(facts["application_region"])
            except CreditCardRepositoryError:
                return self._repository_unavailable(task, extracted, basis)
            catalog = self._combined(catalogs)
            if not catalog.products:
                return self._empty_catalog(task, catalog, extracted, basis)
            missing = []
            prompts, options = {}, dict(QUESTION_OPTIONS)
            if facts.get("applicant_age") is None and facts.get("applicant_age_band") is None:
                missing.append("applicant_age_band")
                prompts["applicant_age_band"] = "您目前是否已满 18 岁？"
            missing.append("annual_income_band_hkd")
            prompts["annual_income_band_hkd"] = "请选择您的税前年收入区间；选项按当前产品门槛生成。"
            options["annual_income_band_hkd"] = _income_options(catalog.products)
            return self._needs_input(
                task, request, extracted, basis, missing, prompts, options)

        if facts.get("applicant_age") is None and facts.get("applicant_age_band") is None:
            return self._needs_input(task, request, extracted, basis, ["applicant_age_band"],
                                     {"applicant_age_band": "您目前是否已满 18 岁？"}, QUESTION_OPTIONS)

        if catalogs is None:
            try:
                catalogs = await self._catalogs(facts["application_region"])
            except CreditCardRepositoryError:
                return self._repository_unavailable(task, extracted, basis)
            catalog = self._combined(catalogs)
            if not catalog.products:
                return self._empty_catalog(task, catalog, extracted, basis)

        gate_products = catalog.products
        requires_residency = any(
            rule.required and rule.rule_type == "residency"
            for product in gate_products for rule in product.eligibility_rules
        )
        if requires_residency and facts.get("hk_resident") is None:
            return self._needs_input(
                task, request, extracted, basis, ["hk_resident"],
                {"hk_resident": (
                    "您目前是否为香港特别行政区居民？汇丰将其列为基本卡申请条件；"
                    "非香港居民仍可尝试申请，但获批几率很低，将按个案审批展示。"
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
        if (has_premier_path and facts.get("hsbc_premier_path") is None
                and facts.get("premier_relationship") is None):
            return self._needs_input(
                task, request, extracted, basis, ["hsbc_premier_path"],
                {"hsbc_premier_path": (
                    "以下哪种汇丰卓越理财情况与您相符？这里的一百万港元指在汇丰的"
                    "总关系结余，不是年收入。"
                )},
                QUESTION_OPTIONS,
            )

        if (facts.get("premier_relationship") is True
                and facts.get("hsbc_total_relationship_balance_hkd") is None
                and facts.get("hsbc_relationship_balance_band") is None):
            return self._needs_input(
                task, request, extracted, basis, ["hsbc_relationship_balance_band"],
                {"hsbc_relationship_balance_band": (
                    "您已确认是汇丰卓越理财客户。为核对 Premier Mastercard 的申请条件，"
                    "请选择您在汇丰的总关系结余区间。"
                )},
                QUESTION_OPTIONS,
            )

        needs_asset = any(needs_asset_information(product, facts) for product in gate_products)
        if needs_asset and facts.get("asset_band") is None:
            return self._needs_input(
                task, request, extracted, basis, ["asset_band"],
                {"asset_band": (
                    "您的收入未达到部分卡的公开门槛。您能否提供最近 3 个月的银行资产证明，"
                    "以及可证明资产大致在哪个区间？这条路径仍需汇丰个案审批。"
                )},
                QUESTION_OPTIONS,
            )

        student_questions = self._student_questions_needed(gate_products, facts)
        if student_questions:
            prompts = {
                "full_time_student": (
                    "您的收入未达到一般信用卡的公开门槛。您是否为全日制学生？"
                    "学生信用卡不设收入要求。"
                ),
                "student_institution_eligible": (
                    "您是否就读于汇丰指定名单内的香港全日制院校课程？"
                ),
            }
            return self._needs_input(
                task, request, extracted, basis, student_questions, prompts, QUESTION_OPTIONS)

        required_usage_fields = ["repayment_mode", "spending_scenarios"]
        scenarios_so_far = facts.get("spending_scenarios") or []
        if isinstance(scenarios_so_far, str):
            scenarios_so_far = [item for item in scenarios_so_far.split(",") if item]
        if "mainland_china" in scenarios_so_far:
            required_usage_fields.append("mainland_payment_method")
        required_usage_fields += ["monthly_card_spend_band_hkd", "reward_preference"]
        missing = [field for field in required_usage_fields if not facts.get(field)]
        if missing:
            prompts = {
                "repayment_mode": "您通常会每月全额还款，还是可能分期或保留余额？",
                "spending_scenarios": (
                    "请按重要程度选择最主要的两到三个消费场景；先选的场景权重更高。"
                    "例如网购、餐饮、旅行、内地消费或学费。"
                ),
                "monthly_card_spend_band_hkd": (
                    "您预计每月信用卡消费大约在哪个区间？这用于比较奖励上限、年费和外币手续费。"
                ),
                "reward_preference": "您更偏好现金回赠、积分、航空里程，还是按预计价值比较？",
                "mainland_payment_method": (
                    "您在中国内地消费时通常怎样付款？Pulse 的 4.4% 是组合上限，"
                    "需要符合手机/二维码支付及自选奖赏类别条件。"
                ),
            }
            return self._needs_input(task, request, extracted, basis, missing, prompts, QUESTION_OPTIONS)

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
        result = recommend(catalog.products, facts, scenarios,
                           facts.get("reward_preference", "no_preference"), now=self.clock())
        if result.primary is None:
            names = {product.card_id: product.product_name for product in catalog.products}
            steps = [
                f"{names.get(card_id, '汇丰信用卡')}：{'；'.join(_public_reason(reason) for reason in reasons)}"
                for card_id, reasons in result.excluded.items()
            ]
            return DomainResult(
                task_id=task.task_id, domain="credit_card", status="answered",
                answer=AnswerBody(
                    summary="根据目前已确认的申请条件，暂时没有可推荐的汇丰信用卡。"
                            "若之后情况有变化，我们可以再为您比较适合的选择。",
                    steps=steps),
                payload={"recommendations": result.model_dump(mode="json"),
                         "catalog": self._catalog_summary(catalog)},
                knowledge_read_id=catalog.read_id, observed_facts=extracted, observed_basis=basis,
            )

        recommendations = [result.primary, *result.supplements, *result.alternatives]
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
        steps = self._format_card_lines(result.primary)
        for card in result.supplements:
            steps.extend(self._format_card_lines(card))
        if facts["repayment_mode"] != "full":
            steps.insert(0, self._format_borrowing_costs(by_id[result.primary.card_id]))
        if result.alternatives:
            steps.append("免年费选择：" + "、".join(card.product_name for card in result.alternatives))
        # Customer-manager advice is strictly an explanation layer.  A failure
        # to read supplementary account data must not interrupt card advice.
        try:
            advisor_brief = await self.advisor.enrich(
                result.primary, by_id[result.primary.card_id], facts, recommendations)
        except Exception:
            advisor_brief = None
        if advisor_brief:
            steps.extend(advisor_brief.steps)
        limitations = list(dict.fromkeys(
            limit for card in recommendations for limit in card.limitations
            if limit and not limit.startswith(("满足", "不满足", "缺少门槛", "非香港居民"))))
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
            answer=AnswerBody(summary=f"主推荐：{result.primary.product_name}", steps=steps,
                              limitations=limitations),
            evidence_refs=evidence,
            next_actions=list(dict.fromkeys(next_actions)),
            notices=[] if product_data_ready else [Notice(
                code="card_product_fields_need_verification",
                message="信用卡奖励和年费字段尚未逐项完成官方核验")],
            payload={"recommendations": result.model_dump(mode="json"), "market": catalog.market,
                     "catalog": self._catalog_summary(catalog),
                     "advisor": advisor_brief.audit if advisor_brief else {
                         "policy_version": "credit-card-advisor-v1", "suppressed": "unavailable"}},
            knowledge_read_id=catalog.read_id, observed_facts=extracted, observed_basis=basis,
        )

    async def _product_question(self, task, request, facts, extracted, basis):
        region = facts.get("application_region")
        markets = list(STUDY_MARKETS.get(region, (region,))) if region else ["CN-HK", "UK", "US"]
        catalogs = []
        for market in dict.fromkeys(market for market in markets if market):
            try:
                catalogs.append(await self.repository.active_products(market, self.clock()))
            except CreditCardRepositoryError:
                continue
        products = [product for catalog in catalogs for product in catalog.products]
        named = _named_card_products(request.text, products)
        if not named:
            return None

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
        try:
            catalogs = await self._catalogs(region)
        except CreditCardRepositoryError:
            return self._repository_unavailable(task, extracted, basis)
        local_catalog = next((item for market, item in catalogs if market == region), None)
        if local_catalog is None or not local_catalog.products:
            combined = self._combined(catalogs)
            if not combined.products:
                return self._empty_catalog(task, combined, extracted, basis)

        # A confirmed age restriction is enough to stop; do not ask for a
        # local account (or spending) if every local product is already ruled out.
        underage = facts.get("applicant_age_band") == "under_18" or (
            facts.get("applicant_age") is not None and facts["applicant_age"] < 18)
        if (underage and local_catalog and local_catalog.products
                and all(assess_eligibility(product, facts).status == "ineligible"
                        for product in local_catalog.products)):
            return self._study_answer(task, catalogs, facts, [], extracted, basis)

        account_field = "us_hsbc_account_path" if region == "US" else "uk_hsbc_account_path"
        if facts.get(account_field) is None:
            # Premier in another country does not establish a local account.
            prompt = (
                "您是否已有英国汇丰卓越理财或私人银行账户？"
                if region == "UK" else
                "为了准确介绍可申请的汇丰美国信用卡，想先确认您是否持有"
                "美国汇丰卓越理财支票账户？"
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
        if ((region == "US" or facts.get(account_field) == "premier_account")
                and facts.get("applicant_age") is None
                and facts.get("applicant_age_band") is None
                and any("applicant_age" in item.missing_fields
                        for item in assessments)):
            return self._needs_input(
                task, request, extracted, basis, ["applicant_age_band"],
                {"applicant_age_band": (
                    "先确认一项申请条件：您目前是否已满 18 岁？"
                    "确认后再按您的使用需求挑选卡片。"
                )}, QUESTION_OPTIONS)
        decision_pair = self._study_decision_pair(local_catalog.products, facts)
        if decision_pair and not facts.get("study_card_focus"):
            lower, premium = decision_pair
            return self._needs_input(
                task, request, extracted, basis, ["study_card_focus"],
                {"study_card_focus": (
                    f"{lower.product_name}年费{self._study_annual_fee_label(lower)}；"
                    f"{premium.product_name}年费{self._study_annual_fee_label(premium)}。"
                    "您希望我们优先按年费成本，还是旅行权益来推荐？"
                )}, QUESTION_OPTIONS)

        raw_scenarios = facts.get("spending_scenarios") or []
        scenarios = raw_scenarios if isinstance(raw_scenarios, list) else \
            [item for item in str(raw_scenarios).split(",") if item]
        return self._study_answer(task, catalogs, facts, scenarios, extracted, basis)

    async def _catalogs(self, region):
        markets = STUDY_MARKETS.get(region, (region,))
        loaded = []
        for market in markets:
            loaded.append((market, await self.repository.active_products(market, self.clock())))
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

    def _study_answer(self, task, catalogs, facts, scenarios, extracted, basis):
        """Local HSBC shelf first; Hong Kong only as a low-approval backup."""
        steps, evidence, limitations = [], [], []
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
            payload={"groups": groups, "market": region},
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
    def _format_card_lines(card):
        """Format a concise, customer-facing recommendation rather than raw DB fields."""
        label = {"primary": "主推荐", "supplement": "场景搭配", "alternative": "其他选择"}[card.role]
        if card.annual_fee is None:
            fee = "请见官网"
        elif card.annual_fee == 0:
            fee = "免年费"
            if card.annual_fee_waiver:
                fee += f"（{_waiver_text(card.annual_fee_waiver)}）"
        else:
            fee = _money(card.annual_fee, card.annual_fee_currency)
            if card.annual_fee_waiver:
                fee += f"（{_waiver_text(card.annual_fee_waiver)}）"
            if (card.ongoing_annual_fee_hkd is not None and card.ongoing_annual_fee_hkd > 0
                    and card.estimated_annual_fee_hkd == 0):
                fee += f"；豁免期后年费每年{card.ongoing_annual_fee_hkd:,.0f}港元"
        perks = [_benefit_text(item) for item in card.main_benefits[:3]]
        scenes = [_category_text(s) for s in card.matched_scenarios if s]
        miles = card.reward_units.get("miles")
        points = card.reward_units.get("points")
        if miles is not None and miles > 0:
            perks.append(f"约 {miles:,.0f} 飞行里程/年")
        elif points is not None and points > 0:
            perks.append(f"约 {points:,.0f} 积分/年")
        perk_text = "；".join(perks) if perks else "详见官网奖赏与附加权益"
        scene_text = "、".join(scenes)
        if card.role == "supplement" and scene_text:
            opening = f"可以和主卡搭配使用，专门补足您在{scene_text}方面的需求。"
        elif scene_text:
            opening = f"如果您希望把{scene_text}用得更划算，这张卡会是更贴合的选择。"
        else:
            opening = "综合您的申请条件和消费习惯，这张卡的整体价值更突出。"

        value_points = []
        if card.estimated_annual_reward_hkd is not None and card.estimated_annual_reward_hkd > 0:
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
    def _student_questions_needed(products, facts) -> list[str]:
        """Ask about student status only when income rules out non-student cards.

        The student card is the only eligible route for applicants below the
        lowest non-student income threshold, so mainstream applicants never
        see these questions.
        """
        student_ids = {product.card_id for product in products if any(
            rule.required and rule.rule_type == "student_status"
            for rule in product.eligibility_rules)}
        if not student_ids:
            return []
        non_student_thresholds = [
            rule.value_numeric for product in products if product.card_id not in student_ids
            for rule in product.eligibility_rules
            if rule.required and rule.rule_type == "minimum_income"
            and rule.value_numeric is not None]
        if not non_student_thresholds:
            return []
        threshold = min(non_student_thresholds)
        income = facts.get("annual_income_hkd")
        if income is not None:
            below = Decimal(str(income)) < threshold
        else:
            band = str(facts.get("annual_income_band_hkd") or "")
            parts = band.split("_")
            if parts[0] == "lt":
                below = Decimal(parts[1]) <= threshold
            elif parts[0] == "range":
                below = Decimal(parts[2]) < threshold
            else:
                below = False
        if not below:
            return []
        if facts.get("full_time_student") is True:
            return (["student_institution_eligible"]
                    if facts.get("student_institution_eligible") is None else [])
        if facts.get("full_time_student") is False:
            return []
        return ["full_time_student"]

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
        return DomainResult(
            task_id=task.task_id, domain="credit_card", status="needs_input",
            answer=AnswerBody(summary="还需要一些信息才能筛选信用卡。",
                              steps=["先确认申请条件，只追问会影响筛选结果的偏好。"]),
            missing_fields=missing,
            follow_up_questions=[_question(
                field, prompts[field], request.profile_version, options=options[field],
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
    def _empty_catalog(task, catalog, extracted, basis):
        return DomainResult(
            task_id=task.task_id, domain="credit_card", status="cannot_answer",
            answer=AnswerBody(summary="目前没有该地区有效的信用卡产品资料。"),
            next_actions=["接入该地区信用卡产品及门槛、奖励、费用和权益数据。"],
            knowledge_read_id=catalog.read_id, observed_facts=extracted, observed_basis=basis,
        )
