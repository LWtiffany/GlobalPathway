"""HSBC account-opening customer service, grounded in reviewed account rows."""

from __future__ import annotations

import json
import re

from datetime import datetime, timezone

from agent2 import clarify
from agent2.llm.account_subintent import AccountSubintentClassifier
from agent2.llm.contracts import StructuredModel
from agent2.llm.prompts import PromptRenderer
from agent2.llm.schemas import WrittenAnswer
from agent2.llm.validation import validate_written_answer
from agent2.models import AnswerBody, Citation, FollowUpQuestion, Notice

from ..contracts import DomainResult, DomainTask, OrchestrationInput
from .bank_account_answer import MARKET_LABEL, build_answer, evidence
from .bank_account_dialogue import (answer_mode_for, dialogue_act_from_capabilities,
                                    exclusion_ids, infer_goal, load_account_state,
                                    pack_account_state, product_relationships, reduce_account_state,
                                    relationship_messages)
from .bank_account_engine import (assess_product, evaluate_decision_path, normalize_product,
                                   normalize_products, positioning_for, product_order,
                                   PRODUCT_POSITIONING)
from .bank_account_plan import build_opening_plan
from .bank_account_repository import BankAccountRepositoryError, RdsBankAccountRepository
from .minimal import catalog_question, merged_facts, needs_input

PROMPT_VERSION = "write-bank-answer-v3"
ACCOUNT_LABELS = {"account_necessity", "product_selection", "eligibility",
                  "documents_process", "fees_balance", "application_issue"}
FACT_TYPES = {"account_region": str, "account_product": str, "applicant_age": int,
              "applicant_age_band": str,
              "account_applicant_relation": str, "uk_residency_years": (int, float),
              "identity_document_type": str, "has_hk_identity_card": bool,
              "tax_certificate_or_ssn": bool, "application_location": str,
              "qualifying_course_offer": bool, "guardian_present": bool,
              "existing_hsbc_hk_customer": str, "existing_hsbc_uk_customer": str,
              "existing_hsbc_us_customer": str, "parent_relationship_status": str,
              "parent_relationship_market": str}
QUESTION_TEXT = {
    "uk_residency_years": "实际开户人在英国或指定属地住了几年？这会影响学生账户资格。",
    "identity_document_type": "实际开户人用香港身份证、护照，还是往来港澳通行证申请？",
    "tax_certificate_or_ssn": "申请人有 SSN，或可提交有效税务证明吗？",
    "qualifying_course_offer": "申请人有符合该账户条件的课程录取证明吗？",
    "guardian_present": "未成年开户人到网点时会有家长或监护人陪同吗？",
}

INCOME_MENTION = re.compile(r"年收入|年薪|税前收入|月收入|\bincome\b", re.I)

# Income and relationship criteria differ by market and by relationship holder.
PREMIER_INCOME_CLARIFICATION = {
    "CN-HK": "补充一点：香港汇丰 Premier 的资格是按「总关系结余」判定的——最近三个月平均约 100 万港元，"
             "或您已在其他国家/地区持有合资格的汇丰 Premier（International Premier）；"
             "年收入本身不是官网列出的开户门槛。方便的话可以告诉我您大致能维持的存款/投资结余，我再据此判断。",
    "US": "补充一点：美国汇丰 Premier 是按关系条件豁免月费的——合格存款及投资总关系余额达 10 万美元、"
          "每月合资格直接入账达 5,000 美元、持有汇丰美国按揭，或为汇丰私人银行客户，满足其一即可；"
          "年收入本身不是官网列出的门槛。",
    "UK": "英国 Premier 的公开资格有不同路径：个人年收入至少 100,000 英镑并存入该账户，"
          "或在汇丰英国持有至少 100,000 英镑存款或投资，或本人已符合其他国家的 Premier 资格。"
          "家长符合资格不代表孩子自动符合，仍需核对孩子本人的申请路径。",
}


def _premier_income_note(region: str, assessments: list[dict], text: str,
                         facts: dict) -> str | None:
    """Correct an income-based Premier assumption, grounded in published criteria."""
    income_stated = bool(INCOME_MENTION.search(text)) \
        or facts.get("annual_income_hkd") is not None \
        or facts.get("annual_income_band_hkd") is not None
    if not income_stated:
        return None
    premier_in_focus = any(
        item["product"].get("customer_segment") == "premier"
        or "premier" in item["product"]["account_id"]
        for item in assessments)
    if not premier_in_focus:
        return None
    return PREMIER_INCOME_CLARIFICATION.get(region)


def _writer_assessments(assessments: list[dict], labels: list[str]) -> list[dict]:
    """Only expose rows needed for this turn so the writer cannot expand scope."""
    scoped = []
    for item in assessments:
        copy = dict(item)
        if "fees_balance" not in labels:
            copy["fees"] = []
            copy["fee_waivers"] = []
        if "documents_process" not in labels:
            copy["documents"] = []
        scoped.append(copy)
    return scoped


def _english_content(node) -> str:
    """Collect the string *values* of the writer payload (skipping dict keys,
    which are structural English like "product_name")."""
    parts: list[str] = []

    def walk(item) -> None:
        if isinstance(item, str):
            parts.append(item)
        elif isinstance(item, dict):
            for value in item.values():
                walk(value)
        elif isinstance(item, (list, tuple)):
            for value in item:
                walk(value)

    walk(node)
    return " ".join(parts)


def _validate_writer_scope(candidate: AnswerBody, labels: list[str], *,
                           selection_limited: bool = False,
                           allowed_product_names: list[str] | None = None,
                           supported_text: str = "",
                           query_text: str = "", family_relationship: bool = False) -> None:
    text = " ".join([candidate.summary, *candidate.steps])
    # A question about 门槛/条件/资格 may legitimately need the fee-waiver
    # balance (e.g. "Premier 有什么开户门槛" = TRB + below-balance fee);
    # only block fee expansion when the question itself is fee-agnostic.
    fee_agnostic_question = not re.search(
        r"门槛|费用|多少钱|月费|结余|豁免|收费|条件|资格", query_text or "")
    # Negated fee statements ("无月费", "不收账户费用", "无最低结余要求") close
    # the topic instead of expanding into it — strip them before the fee check,
    # so a writer that reassures "no fees" is not mistaken for fee drift.
    # App feature wording ("查余额") is likewise not fee content.
    fee_text = re.sub(r"查(?:询|看)?余额|查询?.{0,4}余额", "", text)
    fee_text = re.sub(
        r"(?:无|没有|不收取?|免收?|不存在)[^，。；,.]{0,16}?(?:月费|费用|收费|结余|余额)",
        "", fee_text)
    if "fees_balance" not in labels and fee_agnostic_question and \
            re.search(r"月费|收费|费用|结余|余额|豁免", fee_text):
        raise ValueError("writer expanded into unrequested fee information")
    if selection_limited:
        if re.search(r"(?:推荐|首选|最适合|应该|建议)\s*(?:你)?(?:开|选择|办理)|就开", text):
            raise ValueError("writer recommended from an incomplete product catalog")
        if not re.search(r"(?:目前|当前|现有).*(?:收录|资料|候选).*(?:不足|只|无法|不能)|"
                         r"(?:无法|不能).*(?:完整)?(?:比较|推荐|判断)", text):
            raise ValueError("writer omitted the incomplete-catalog disclosure")
    language_check = text
    # The prompt allows product names in their official (English) language; the
    # exemption list must therefore cover the whole catalog, not just the
    # products selected this turn (e.g. comparison answers cite other names).
    for product_name in allowed_product_names or []:
        language_check = language_check.replace(product_name, "")
    language_check = re.sub(r"\b(?:HSBC|SSN|UCAS|HKID|App)\b", "", language_check,
                            flags=re.I)
    if family_relationship:
        if re.search(r"作为.{0,15}Premier客户|凭.{0,12}Premier身份|免费开立|提前领取银行卡|抵英后即可|寄至|首周|录取信|租房合同|Next Generation|Junior Pack", text, re.I):
            raise ValueError("unsupported family entitlement or fulfilment promise")
        if re.search(r"可为[她他].{0,8}安排|符合.{0,12}要求.{0,10}可申请", text):
            raise ValueError("family service described as confirmed eligibility")
    if supported_text:
        # English words present in the fed data (product names, account types,
        # rule terms) may be reused when the writer cites a proper noun such as
        # "HSBC UK Current Account". Only English wording absent from the fed
        # data counts as copied untranslated source text.
        vocab = {word.lower() for word in re.findall(r"[A-Za-z]{2,}", supported_text)}

        def _known_run(match: re.Match) -> str:
            words = re.findall(r"[A-Za-z]{2,}", match.group(0))
            return "" if all(word.lower() in vocab for word in words) else match.group(0)

        language_check = re.sub(r"[A-Za-z]{2,}(?:[\s&'/-]+[A-Za-z]{2,})*",
                                _known_run, language_check)
    if re.search(r"\b(?:[A-Za-z]{2,}\s+){2,}[A-Za-z]{2,}\b", language_check):
        raise ValueError("writer copied an untranslated English sentence")


def _fallback_labels(text: str) -> list[str]:
    patterns = (
        ("application_issue", r"失败|被拒|过不去|提交不了|补交|没消息|取消|审核未通过|识别不了"),
        ("fees_balance", r"费用|收费|月费|管理费|最低存款|最低余额|最低要存|要存.*钱|结余|豁免|免掉|门槛"),
        ("product_selection", r"哪个|哪种|怎么选|适合|比较|区别|还是普通账户|有什么账户|"
                              r"开(?:什么|哪种)户|别的账户|其他账户|换一个|另外.*账户|"
                              r"储蓄账户|定期存款|支票账户|往来账户"),
        ("eligibility", r"能不能|能否|能开|可以.*开|必须|要住满|没住满|几岁|未满|资格|没有.*能"),
        ("documents_process", r"材料|证件|证明|流程|怎么开|如何开|怎么申请|预约|app|网上|线上|网点|分行"),
        ("account_necessity", r"有必要|需要.*(?:账户|开户)|(?:要不要|需不需要|是否需要|必须先|需要先|要先).*(?:开户|开账户)|一定要|现在就要|出发前.*到了"),
    )
    return [label for label, pattern in patterns if re.search(pattern, text, re.I)] or ["documents_process"]




def _family_scenario(text: str) -> str | None:
    patterns = (
        ("handoff", r"客户经理|转介|对接当地|交接摘要"),
        ("funding", r"汇.*(?:学费|生活费)|给孩子.*(?:汇|转).*(?:钱|学费|生活费)"),
        ("fees", r"月费|管理费|免.*费|免.*管理费|关系余额|放一笔钱|资金|存款|豁免|需要自己放|钱吗"),
        ("maintenance", r"满\s*18岁|转去美国|毕业|回国|账户怎么办"),
        ("activation", r"卡没收到|借记卡没|账户已经开|账户已开|激活|马上用"),
        ("source_of_funds", r"没有收入|资金来源|解释资金|补件|开户受阻|申请被拒"),
        ("eligibility_documents", r"没有当地地址|没有.*税号|先申请|资格|需要什么材料|(?:便利|权益).*(?:材料)|材料.*(?:便利|权益)"),
        ("minor", r"未成年|\d{1,2}岁|不陪同|谁能帮.*开"),
        ("timeline", r"出发|八月份|八月|\d{1,2}月|国内先办|来得及"),
        ("use_case", r"生活费|房租|日常消费|收款|交房租|开什么合适"),
        ("family_setup", r"一家|家庭|准备哪些账户|准备.*账户|孩子去.*读书|孩子.*留学"),
    )
    for scenario, pattern in patterns:
        if re.search(pattern, text):
            return scenario
    if re.search(r"孩子|子女|儿子|女儿|未成年", text):
        return "family_setup"
    return None


def _family_scenario_answer(*, scenario: str | None, text: str, region: str,
                            facts: dict, context: dict, catalog_available: bool) -> AnswerBody | None:
    market = MARKET_LABEL[region]
    age = facts.get("applicant_age")
    location = facts.get("application_location")
    location_name = {"CN": "中国内地", "CN-HK": "香港", "UK": "英国", "US": "美国"}.get(location)
    location_question = "孩子准备申请时会在中国内地、香港，还是已经在目的地国家？"
    if scenario == "family_setup":
        summary = "先按家庭分工整理账户用途，不用先为每个家人各开一款账户。"
        steps = ["先明确实际开户人通常是留学生本人；家长账户用于供款，孩子账户用于收款、房租和日常消费，两者不应混作一个申请人。",
                 "再核对孩子是否已有可用的当地账户、银行卡或付款安排；只有缺少必要的本地收付款能力时，才进一步比较开户。",
                 f"目的地暂按{market}理解；如果不是这个地区请纠正。{location_question}"]
        if not age:
            steps.append("孩子多大？年龄会影响可申请的账户、监护和陪同要求。")
        return AnswerBody(summary=summary, steps=steps)
    if scenario == "handoff":
        return AnswerBody(summary="可以整理一份交给现有客户经理核对的咨询摘要；目前不能确认已具备正式跨境转介。",
                          steps=["说明孩子本人将在哪个地区开户、年龄、申请所在地和计划出发时间。",
                                 "请客户经理确认能否协助联系目的地实体、孩子适用的账户、申请渠道、本人到场要求和收卡激活安排。",
                                 "家长的现有关系只作为核对线索，不代表孩子已获资格或费用豁免；本次没有替您发送或预约。",
                                 location_question])
    if scenario == "funding":
        return AnswerBody(summary="开户后给孩子汇学费和生活费，先区分学校收款与孩子个人账户收款。",
                          steps=["学费应先核对学校发票上的收款人、账户和币种；学校直接收款与汇入孩子个人账户不是同一条路径。",
                                 "生活费再核对孩子个人账户的账户名、币种、可收款方式及家长汇出所需信息。",
                                 "准备汇款前核对金额、用途、收款信息及可能的银行费用；不要只凭账户已开通就假设款项可即时使用。"])
    if scenario == "timeline":
        return AnswerBody(summary="可以按出发时间倒排，但申请、审核、开户、收卡和激活不能视为同一个完成节点。",
                          steps=["先确认孩子从当前所在地申请目的地账户的渠道是否开放、申请人是否需到场。",
                                 "整理孩子身份证明、现有地址证明、学生身份或录取材料；具体必需项按适用渠道确认。",
                                 "分别向银行确认审核时长、开户生效、卡片寄送地点和激活要求；目前不能保证出发前全部完成。",
                                 location_question])
    if scenario == "use_case":
        return AnswerBody(summary=f"针对生活费、房租和日常消费，优先核对{market}账户是否支持孩子需要的本地收款、转账及借记卡使用，再比较具体产品。",
                          steps=["先确认孩子是否需要本地账户收款和本地转账，房租收款方是否接受该账户付款。",
                                 "核对借记卡、网银/手机银行、多币种及账户费用；普通往来账户若满足这些需求，未必需要高等级关系账户。",
                                 "家长的私行关系是否给孩子带来可核实的服务便利，需另向客户经理确认。",
                                 f"要按资格筛选具体产品，还需确认孩子年龄和申请所在地（当前记录：{location_name or '待确认'}）。"])
    if scenario == "minor":
        return AnswerBody(summary=f"{age or '未成年'}岁开户要按孩子本人作为账户持有人核对{market}当地未成年人规则，家长能否代办或不陪同不能跨市场推断。",
                          steps=["先确认申请账户的持有人是孩子本人，家长是监护人或协助人，不应把账户归属说成家长代持。",
                                 "请确认孩子的申请所在地，以及当地银行对监护人同意、到场陪同、身份证件和地址证明的要求。",
                                 "目前没有依据确认家长不陪同仍可开户，也不应直接推荐面向成年人的学生账户。",
                                 location_question])
    if scenario == "eligibility_documents":
        summary = ("现有私行或家庭关系可能提供咨询便利，但不能据此确认孩子已有开户资格；材料和渠道需按孩子本人核对。"
                   if re.search(r"便利|权益", text) else
                   "没有当地地址或税号时能否先申请，取决于孩子、目的地账户和具体申请渠道；不能把常见材料清单当成统一门槛。")
        return AnswerBody(summary=summary,
                          steps=["先区分问题发生在申请资格、身份证明、地址证明，还是税务信息；税号要求因地区和账户而不同。",
                                 "请确认孩子计划从哪里申请，以及目前已有的身份证件、常住地址证明和学生材料。",
                                 "向该渠道核实哪些材料必须提交、哪些可以接受替代文件、哪些可在开户后补交；未经确认不要自行假设可后补。",
                                 location_question])
    if scenario == "source_of_funds":
        return AnswerBody(summary="孩子没有个人收入不等于资金来源问题无法说明；应如实说明家庭资助关系，并按银行要求提供可核实材料。",
                          steps=["先区分银行要求的是申请人收入、账户资金来源，还是家长汇款来源；这几项不能互相替代。",
                                 "可先准备家长资助说明、亲属关系证明及与资金来源相符的文件，具体格式和是否需要翻译由经办银行确认。",
                                 "不要编造孩子收入或承诺一定通过审核；若已收到补件通知，按通知原文逐项核对并联系经办渠道。"])
    if scenario == "activation":
        return AnswerBody(summary="账户已开不一定代表借记卡已送达或可以立即完成所有付款。",
                          steps=["核对开户状态、卡片是否已制发、寄送地址和预计送达时间，并确认是否需要本人签收。",
                                 "同时确认网银/手机银行是否已激活、手机号能否接收验证码、卡片是否需要单独启用。",
                                 "在当地付款能力确认前，不要假定落地即可刷卡或转账；可向银行核实临时付款方式和联系路径。"])
    if scenario == "maintenance":
        return AnswerBody(summary="年龄、留学目的地或毕业身份变化后，账户处理取决于现有账户条款和服务地区，不能直接推断要转换或关闭。",
                          steps=["先确认孩子目前持有的具体账户、开户地区、登记居住地址和联系方式。",
                                 "再请该账户所属银行核实满18岁或迁往美国后是否影响账户类别、费用、税务信息、卡片和网银。",
                                 "毕业回国时再核实是否可保留、需要更新资料或申请关闭，并取得正式办理要求。"])
    return None


def _explicit_facts(text: str) -> dict:
    facts = {}
    folded = text.casefold()
    destination = re.search(r"(?:去|赴|到|前往|转去)\s*(英国|美国|香港).{0,14}(?:留学|读书|上学|工作|开户|开账户|读研)", text)
    if destination:
        facts["account_region"] = {"英国": "UK", "美国": "US", "香港": "CN-HK"}[destination[1]]
    elif re.search(r"(?:英国|美国|香港).{0,8}(?:账户|开户|读书|留学|读研)", text):
        market = re.search(r"英国|美国|香港", text)
        facts["account_region"] = {"英国": "UK", "美国": "US", "香港": "CN-HK"}[market[0]]
    relationship = re.search(
        r"(?:我是|我有|持有|现有).{0,12}(香港|英国|美国).{0,12}(私行|私人银行|Premier|卓越理财)", text, re.I)
    if relationship:
        facts["parent_relationship_market"] = {"香港": "CN-HK", "英国": "UK",
                                              "美国": "US"}[relationship[1]]
        facts["parent_relationship_status"] = "user_stated_unverified"
    if not facts.get("account_region") and ("hsbc one" in folded or "汇丰 one" in folded):
        facts["account_region"] = "CN-HK"
    elif not facts.get("account_region") and "premier checking" in folded:
        facts["account_region"] = "US"
    location = re.search(
        r"国内先|国内申请|国内办理|(?:现在|目前)(?:在)?\s*(香港|英国|美国|内地|中国|国内)|(?:身在|身处|人在|住在|申请时在|从)\s*(香港|英国|美国|内地|中国|国内)",
        text)
    if location and location[0].startswith("国内"):
        facts["application_location"] = "CN"
    elif location:
        location_name = location[1] or location[2]
        facts["application_location"] = {"香港": "CN-HK", "英国": "UK", "美国": "US",
                                         "内地": "CN", "中国": "CN", "国内": "CN"}[location_name]
    if re.search(r"孩子|儿子|女儿|子女|未成年", text):
        facts["account_applicant_relation"] = "child"
        facts["applicant_identity"] = "student"
    age_pattern = (r"(?:孩子|儿子|女儿)[^，。；]{0,8}?(\d{1,2})\s*岁"
                   if re.search(r"孩子|儿子|女儿", text) else r"(?<!\d)(\d{1,2})\s*岁")
    age = re.search(age_pattern, text)
    if age:
        facts["applicant_age"] = int(age.group(1))
    region_name = re.search(r"英国|美国|香港", text)
    if region_name and re.search(r"开户|账户|HSBC\s*(?:One|Premier)", text, re.I) and not relationship:
        facts["account_region"] = {"英国": "UK", "美国": "US", "香港": "CN-HK"}[region_name[0]]
    return_to_market = re.search(r"(?:回到|回头说|继续说|再说|切回)\s*(英国|美国|香港)", text)
    if return_to_market:
        facts["account_region"] = {"英国": "UK", "美国": "US", "香港": "CN-HK"}[return_to_market[1]]
    if re.search(r"(?:未满|不满)\s*18\s*岁", text):
        facts.pop("applicant_age", None)
        facts["applicant_age_band"] = "under_18"
    if re.search(r"刚到英国|刚落地英国", text):
        facts["uk_residency_years"] = 0
    years = re.search(r"(?:在英国|英国).*?(?:住了|居住了|住满)\s*(\d+)\s*年", text)
    if years:
        facts["uk_residency_years"] = int(years.group(1))
    if re.search(r"孩子|儿子|女儿|子女|未成年", text):
        facts["account_applicant_relation"] = "child"
        facts["applicant_identity"] = "student"
        location = re.search(
            r"国内先|国内申请|国内办理|(?:现在|目前)(?:在)?\s*(香港|英国|美国|内地|中国|国内)|(?:身在|身处|人在|住在|申请时在|从)\s*(香港|英国|美国|内地|中国|国内)",
            text)
        if location and location[0].startswith("国内"):
            facts["application_location"] = "CN"
        elif location:
            location_name = location[1] or location[2]
            facts["application_location"] = {"香港": "CN-HK", "英国": "UK", "美国": "US",
                                             "内地": "CN", "中国": "CN", "国内": "CN"}[location_name]
    if re.search(r"没有香港身份证|没香港身份证", text):
        facts["has_hk_identity_card"] = False
    elif re.search(r"有香港身份证|持有香港身份证", text):
        facts["has_hk_identity_card"] = True
    if "通行证" in text:
        facts["identity_document_type"] = "eep"
    elif "护照" in text:
        facts["identity_document_type"] = "passport"
    elif facts.get("has_hk_identity_card") is True:
        facts["identity_document_type"] = "hkid"
    if re.search(r"有税务证明|有有效税务证明|有\s*SSN", text, re.I):
        facts["tax_certificate_or_ssn"] = True
    return facts


def _model_facts(text: str, candidates) -> dict:
    facts = {}
    for item in candidates:
        field, value = item.field_code, item.value
        if item.evidence.casefold() not in text.casefold():
            continue
        expected_type = FACT_TYPES[field]
        if isinstance(value, bool) and expected_type is not bool:
            continue
        if not isinstance(value, expected_type):
            continue
        if field == "account_region" and value not in MARKET_LABEL:
            continue
        if field == "identity_document_type" and value not in ("hkid", "passport", "eep"):
            continue
        if field == "account_applicant_relation" and value not in ("self", "child", "parent"):
            continue
        if field == "application_location" and not re.search(
                r"(?:现在|目前|身在|身处|人在|申请时|从|住在|居住在|在).{0,5}(?:香港|英国|美国|内地|中国)",
                item.evidence):
            continue
        if field in ("applicant_age", "uk_residency_years") and not 0 <= value <= 119:
            continue
        facts[field] = value
    return facts


def _question(field: str, profile_version: int) -> FollowUpQuestion:
    question = catalog_question(field, profile_version)
    if field in QUESTION_TEXT:
        question = question.model_copy(update={"text": QUESTION_TEXT[field],
                                               "rule": "bank_account_rule_v2"})
    return question


ACCOUNT_FOLLOWUP_PRIORITY = (
    "applicant_identity", "applicant_age", "has_hk_identity_card",
    "guardian_present", "uk_residency_years", "qualifying_course_offer",
    "tax_certificate_or_ssn", "existing_hsbc_hk_customer",
    "application_location", "identity_document_type",
)


def _missing_account_questions(assessments: list[dict]) -> list[str]:
    """Return every user-answerable unknown criterion in a stable order.

    Internal bank-review markers are deliberately absent from ``clarify.FIELDS``
    and therefore never become fake questions for the user.
    """
    unknown_entries = [
        entry for item in assessments
        for entry in [*item["rule_results"],
                      *(rule for route in item["routes"] for rule in route["rule_results"])]
        if entry.get("result") == "unknown" and entry.get("rule") is not None
    ]
    raw_fields = {entry["rule"].get("field_code") for entry in unknown_entries}
    ordered = [field for field in ACCOUNT_FOLLOWUP_PRIORITY
               if field in raw_fields and field in clarify.FIELDS]
    # A published 18-year boundary can be answered accurately with two clear
    # choices. Do not pretend to know an exact age from that selection.
    if "applicant_age" in ordered:
        age_entries = [entry for entry in unknown_entries
                       if entry["rule"].get("field_code") == "applicant_age"]
        thresholds = {str(entry["rule"].get("value_numeric")) for entry in age_entries}
        if thresholds <= {"18", "18.0"}:
            ordered[ordered.index("applicant_age")] = "applicant_age_band"
    return list(dict.fromkeys(ordered))


def _kind(product: dict) -> str:
    account_id = product.get("account_id", "")
    if "global_money" in account_id:
        return "多币种补充账户"
    if any(token in account_id for token in ("_cd", "time_deposit", "fixed_rate")):
        return "定期存款"
    return {
        "checking": "支票账户", "current": "往来账户", "integrated": "综合账户",
        "savings": "储蓄账户", "fixed_deposit": "定期存款", "time_deposit": "定期存款",
        "cd": "定期存款",
    }.get(product.get("account_type"), "银行账户")


def _discovery_products(products: list[dict], goal: str) -> list[dict]:
    def fixed(row: dict) -> bool:
        return any(token in row.get("account_id", "")
                   for token in ("_cd", "time_deposit", "fixed_rate"))

    if goal == "fixed_deposit":
        return [row for row in products if fixed(row)]
    if goal == "savings":
        return [row for row in products
                if row.get("account_type") == "savings" and not fixed(row)]
    if goal == "daily_banking":
        return [row for row in products
                if row.get("account_type") in {"checking", "current", "integrated"}
                and not fixed(row)]
    return list(products)


def _discovery_answer(assessments: list[dict], region: str, *,
                      total: int, goal: str, facts: dict | None = None) -> AnswerBody:
    products = [item["product"] for item in assessments]
    market = MARKET_LABEL[region]
    facts = facts or {}
    persona = positioning_for(facts)
    if goal == "unknown":
        summary = (f"您想在{market}汇丰开立账户——可选的账户共有 {total} 款，"
                   f"结合您的情况（{persona}），我先为您重点介绍其中三款：")
    else:
        goal_label = {"daily_banking": "日常收付款", "savings": "储蓄",
                      "fixed_deposit": "定期存款"}[goal]
        summary = (f"按您“{goal_label}”的需求（{persona}），我从{market}汇丰共 {total} 款账户中"
                   f"为您筛选出 {len(products)} 款最贴合的，先介绍前三款：")
    steps: list[str] = []
    for item in assessments[:3]:
        product = item["product"]
        info = PRODUCT_POSITIONING.get(product["account_id"], {})
        line = f"{product['product_name']}——适合{info.get('for', _kind(product))}"
        for point in info.get("points", [])[:2]:
            line += f"；{point}"
        relationship = relationship_messages(item.get("relationships", []))
        if relationship:
            line += "；" + " ".join(relationship).rstrip("。")
        elif item["status"] == "needs_input":
            line += "；能否办理还需要核对您的具体条件"
        steps.append(line.rstrip("。") + "。")
    steps.append(_action_step(region, facts))
    steps.append("如果您告诉我更具体的安排（比如主要用途、大致的资金情况），"
                 "我可以为您锁定其中一款，并把开户条件和所需材料一并整理给您。")
    return AnswerBody(
        summary=summary,
        steps=steps,
        limitations=["这里只提供开户咨询；实际资格和审批结果以汇丰办理时的信息为准。",
                     "推荐理由来自已核验的汇丰官网页面，产品或优惠有变动时应重新核对。"],
    )


def _action_step(region: str, facts: dict) -> str:
    """Concrete next-step guidance, sourced from the archived official pages."""
    if region != "CN-HK":
        return ("如果您已经确定了，直接在对应地区的汇丰官网或 App 提交申请即可；"
                "需要我为您核对具体的开户条件，随时告诉我。")
    if facts.get("applicant_age") is not None and facts["applicant_age"] < 18:
        return ("未满 18 岁需要到网点办理：11-17 岁请由家长或监护人陪同前来，"
                "11 岁以下由家长以本人名义代开；请携带孩子的身份证明和家长的有效证件。")
    return ("如果您已经确定，汇丰香港 App 开户大约 5 分钟即可完成"
            "（适用于 18-75 岁且暂未持有汇丰账户；如情况不同，到网点或找 Digital Banker 同样可以办理）。")


def _necessity_answer(region: str) -> AnswerBody:
    market = MARKET_LABEL[region]
    return AnswerBody(
        summary=f"不一定要先开户。去{market}留学本身不代表必须在出发前开好当地汇丰账户。",
        steps=[
            "先看您到达后是否需要用当地账户收生活费、支付日常开支，以及目前是否已有可用的银行卡或付款安排。",
            "如果暂时没有明确的收付款需求，可以先不急着选产品；如果需要开户，再按具体账户核对资格、材料和申请渠道。",
            "您问的是“出发前必须开好”，还是“到了当地后要不要开”？这两种情况的准备方式不同。",
        ],
        limitations=["具体账户资格、申请渠道和审批结果以汇丰官网及办理时审核为准。"],
    )


def _catalog_answer(products: list[dict], region: str, act: str, *,
                    total: int, exhausted: bool = False) -> AnswerBody:
    categories: dict[str, int] = {}
    for row in products:
        categories[_kind(row)] = categories.get(_kind(row), 0) + 1
    if act == "ask_catalog_scope":
        summary = (f"不止刚才提到的账户。按目前已核验的官网资料，"
                   f"{MARKET_LABEL[region]}汇丰共收录 {total} 个个人存款产品。")
    elif exhausted:
        summary = (f"这一轮没有新的候选了；前面已经覆盖了当前收录的"
                   f"{MARKET_LABEL[region]}汇丰个人存款产品。")
    else:
        summary = "可以，先不考虑刚才提到的账户，我把其他候选为您列出来。"
    steps = [f"{row['product_name']}：{_kind(row)}。" for row in products]
    if categories:
        steps.insert(0, "这次展示的类型包括：" + "、".join(
            f"{name} {count} 个" for name, count in categories.items()) + "。")
    steps.append("您更看重日常收付款、储蓄，还是定期存款？说明用途后，我可以继续为您缩小范围。")
    return AnswerBody(
        summary=summary,
        steps=steps,
        limitations=["这里展示的是已核验的汇丰个人存款产品，不包含企业、投资和已经下架的账户。",
                     "产品条件可能变化，实际申请以汇丰官网和办理时审核为准。"],
    )


def _focus_answer(assessment: dict, region: str, *, confirmed: bool = False) -> AnswerBody:
    product = assessment["product"]
    name = product["product_name"]
    relationships = assessment.get("relationships") or product_relationships(product["account_id"])
    if confirmed:
        summary = f"好，接下来先按 {name} 这个账户继续。"
        steps = [f"后面您说“这个账户”时，我会理解为 {name}。"]
    else:
        summary = f"您问的是 {MARKET_LABEL[region]}汇丰的 {name}，它属于{_kind(product)}。"
        steps = []
    steps.extend(relationship_messages(relationships))
    if assessment["status"] == "needs_input":
        steps.append("是否适合您，还要结合申请人的年龄、身份和银行公布的资格条件判断。")
    steps.append("您可以继续问它的开户条件、所需材料、申请方式或费用；我会沿用这个账户回答。")
    return AnswerBody(
        summary=summary,
        steps=steps,
        limitations=["这里只提供开户咨询；实际申请条件和审批结果以汇丰办理时的信息为准。"],
    )


def _ambiguous_reference_answer(products: list[dict]) -> AnswerBody:
    names = [row["product_name"] for row in products]
    return AnswerBody(
        summary="刚才同时提到了多个账户，我还不能确定您说的“这个”是哪一个。",
        steps=[*(f"候选 {index}：{name}。" for index, name in enumerate(names, 1)),
               "请直接说账户名称，或回复候选编号；确认后我再继续讲开户条件和流程。"],
        limitations=["在账户没有明确前，我不会替您默认选择，以免给错开户建议。"],
    )


def _money(value, currency: str | None) -> str:
    try:
        numeric = float(value)
        amount = f"{int(numeric):,}" if numeric.is_integer() else f"{numeric:,.2f}".rstrip("0").rstrip(".")
    except (TypeError, ValueError, OverflowError):
        amount = str(value)
    unit = {"HKD": "港元", "USD": "美元", "GBP": "英镑"}.get(currency, currency or "")
    return f"{amount} {unit}".strip()


def _comparison_answer(assessments: list[dict], region: str, facts: dict) -> AnswerBody:
    """Deterministic, row-grounded comparison for explicitly named products."""
    names = [item["product"]["product_name"] for item in assessments]
    steps: list[str] = []
    for item in assessments:
        product = item["product"]
        info = PRODUCT_POSITIONING.get(product["account_id"], {})
        parts = [f"定位：适合{info.get('for', _kind(product))}"]
        if info.get("points"):
            parts.append(info["points"][0])
        candidate_rules = [entry["rule"] for entry in item["rule_results"]] + \
            list(item.get("fee_waivers", []))
        balance_rules = [rule for rule in candidate_rules
                         if rule.get("currency") and rule.get("value_numeric") is not None]
        if balance_rules:
            rule = balance_rules[0]
            parts.append("公开资金条件：" + _money(rule["value_numeric"], rule.get("currency")))
        fee_lines = []
        if product["account_id"] == "hsbc_hk_one":
            if facts.get("has_hk_identity_card") is True:
                fee_lines.append("香港身份证持有人无总关系结余门槛及低结余费")
            elif facts.get("has_hk_identity_card") is False:
                fee_lines.append("非香港身份证客户于 2026 年 1 月 1 日或之后开户，须维持 10,000 港元总关系结余，否则低结余费为 100 港元/月")
            else:
                fee_lines.append("香港身份证持有人无低结余费；非香港身份证客户于 2026 年 1 月 1 日或之后开户，须维持 10,000 港元总关系结余，否则为 100 港元/月")
        else:
            for fee in item["fees"]:
                if fee.get("amount") is None:
                    continue
                if fee["fee_type"] in {"below_balance", "below_balance_fee"}:
                    fee_lines.append("低结余费 " + _money(fee["amount"], fee.get("currency")) + "/月")
                elif fee["fee_type"] in {"monthly_account", "monthly_fee"} and fee["amount"] == 0:
                    fee_lines.append("不收月费")
        if fee_lines:
            parts.append("费用：" + "、".join(dict.fromkeys(fee_lines)))
        routes = [entry["route"] for entry in item["routes"]]
        if routes:
            channels = []
            for route in routes:
                label = {"mobile_app": "App", "online": "网上", "branch": "网点",
                         "phone": "电话"}.get(route.get("channel"), route.get("channel"))
                if label and label not in channels:
                    channels.append(label)
            if channels:
                parts.append("申请渠道：" + "、".join(channels))
        steps.append(f"{product['product_name']}——" + "；".join(parts) + "。")

    ids = {item["product"]["account_id"] for item in assessments}
    if {"hsbc_hk_one", "hsbc_hk_premier"} <= ids:
        steps.append(
            "怎么选：一般日常收付款、留学缴费和多币种管理，优先看 HSBC One；"
            "若能维持 100 万港元总关系结余，或已在其他国家/地区持有或符合汇丰 Premier 资格，"
            "再看 HSBC Premier 的全球账户管理与跨境服务。"
        )
        if facts.get("has_hk_identity_card") is False:
            steps.append("您若没有香港身份证，还要留意 HSBC One 自 2026 年 1 月 1 日起的新户低结余费规则。")
    else:
        steps.append("选择时重点看用途、年龄/身份、资金门槛和申请渠道；这些条件不同，不能只按账户名称判断。")
    return AnswerBody(
        summary=f"{MARKET_LABEL[region]}汇丰的 " + " 与 ".join(names) + " 定位不同，核心差别如下。",
        steps=steps,
        limitations=["这里只提供开户咨询；实际资格和审批结果以汇丰办理时的信息为准。",
                     "资金门槛与费用按已核验官网资料整理，政策变化时应重新核对。"],
    )


class BankAccountWorkflow:
    def __init__(self, *, model: StructuredModel | None = None,
                 renderer: PromptRenderer | None = None, repository=None):
        self.model = model
        self.renderer = renderer or PromptRenderer()
        self.repository = repository or RdsBankAccountRepository()
        self.classifier = AccountSubintentClassifier(model, self.renderer) if model else None

    async def run(self, task: DomainTask, request: OrchestrationInput) -> DomainResult:
        facts, basis = merged_facts(task, request)
        raw_state = request.domain_state.get("bank_account") or {}
        # RDS scopes this JSON to the profile's unique conversation; reject misbound input.
        if raw_state.get("profile_id") not in (None, request.profile_id):
            raw_state = {}
        family_context = dict(raw_state.get("family_context") or {})
        family_signals = r"孩子|子女|儿子|女儿|未成年|留学|读书|读研|出发前|开学前|家庭账户|我们一家|家长|监护人"
        explicit_child = bool(re.search(r"孩子|子女|儿子|女儿|未成年", request.text))
        family_mode = bool(re.search(family_signals, request.text)) or bool(family_context.get("family_mode"))
        scenario = _family_scenario(request.text)
        family_context["family_mode"] = family_mode
        if scenario:
            family_context["scenario"] = scenario
        destination_market = _explicit_facts(request.text).get("account_region")
        explicit_destination = re.search(r"(?:去|赴|到|前往|转去)\s*(英国|美国|香港)|(英国|美国|香港).{0,8}(?:账户|开户|读书|留学|读研)", request.text)
        if explicit_destination:
            destination_name = explicit_destination[1] or explicit_destination[2]
            destination_market = {"英国": "UK", "美国": "US", "香港": "CN-HK"}[destination_name]
        elif re.search(r"孩子.*(?:开户|账户).*(?:有什么便利|权益|私行|客户身份)", request.text):
            destination_market = None
        existing_relationship = re.search(
            r"(?:我是|我有|持有|现有).{0,12}(香港|英国|美国).{0,12}(私行|私人银行|Premier|卓越理财)",
            request.text, re.I)
        if existing_relationship:
            family_context["parent_relationship_statement"] = request.text[:500]
            family_context["relationship_market"] = {"香港": "CN-HK", "英国": "UK",
                                                     "美国": "US"}[existing_relationship[1]]
            family_context["relationship_status"] = "user_stated_unverified"
        family_signal = bool(re.search(r"私行|私人银行|家庭权益|出发前|落地前", request.text))
        family_plan = family_mode or family_signal
        if family_signal:
            family_context["customer_goal"] = request.text[:1000]
        if re.search(r"我.{0,12}(?:私行|私人银行|Premier|卓越理财)", request.text, re.I):
            family_context["parent_relationship_statement"] = request.text[:500]
        if re.search(r"出发前|落地前|开学前|到英国前|到美国前", request.text):
            departure = re.search(r"(\d{1,2})\s*月", request.text)
            family_context["target_milestone"] = "出发前" + (departure[0] if departure else "完成开户准备")
        # The current turn must beat the previous turn's stored query scope.
        explicit_seed = _explicit_facts(request.text)
        if re.search(r"孩子.*(?:开户|账户).*(?:有什么便利|权益|私行|客户身份)", request.text):
            explicit_seed.pop("account_region", None)
        destination_match = re.search(r"(?:去|赴|到|前往|转去)\s*(英国|美国|香港).{0,14}(?:留学|读书|上学|工作|开户|开账户|读研)", request.text)
        if destination_match:
            explicit_seed["account_region"] = {"英国": "UK", "美国": "US", "香港": "CN-HK"}[destination_match[1]]
        destination_market = explicit_seed.get("account_region")
        if re.search(r"国内先|在国内|从国内", request.text):
            explicit_seed["application_location"] = "CN"
            facts["application_location"] = "CN"
            basis["application_location"] = "本轮明确表示国内办理"
        if destination_market:
            explicit_seed["account_region"] = destination_market
        market_hint = destination_market or facts.get("account_region")
        state = load_account_state(raw_state, market_hint)
        labels = _fallback_labels(request.text)
        notices: list[Notice] = []
        if self.classifier and request.input_mode == "free_text":
            try:
                classified = await self.classifier.classify(request.text)
                if (classified.service_goal == "family_opening_plan"
                        and classified.goal_evidence
                        and classified.goal_evidence in request.text):
                    family_plan = True
                    family_context["customer_goal"] = request.text[:1000]
                account_labels = [label for label in classified.subintents if label in ACCOUNT_LABELS]
                if "fees_balance" in account_labels and "eligibility" in account_labels and \
                        not re.search(r"能不能开|能否开|能开吗|有资格|开户资格", request.text):
                    account_labels.remove("eligibility")
                if account_labels:
                    labels = account_labels
                    if "product_selection" in _fallback_labels(request.text) and \
                            "product_selection" not in labels:
                        labels.append("product_selection")
                extracted = _model_facts(request.text, classified.facts)
                facts.update(extracted)
                basis.update({field: "本轮用户陈述" for field in extracted})
            except Exception:
                notices.append(Notice(code="account_subintent_fallback",
                                      message="开户问题分类暂用本地规则继续处理。"))
        explicit = _explicit_facts(request.text)
        facts.update(explicit)
        basis.update({field: "本轮用户陈述" for field in explicit})
        if not facts.get("parent_relationship_status") and family_context.get("relationship_status"):
            facts["parent_relationship_status"] = family_context["relationship_status"]
        if not facts.get("parent_relationship_market") and family_context.get("relationship_market"):
            facts["parent_relationship_market"] = family_context["relationship_market"]
        if facts.get("application_location") == facts.get("account_region") and re.search(r"国内先|在国内|从国内", request.text):
            facts["application_location"] = "CN"
        if destination_market:
            facts["account_region"] = destination_market
            basis["account_region"] = "本轮提到的留学或开户目的地"
        if not facts.get("account_applicant_relation") and family_mode:
            facts["account_applicant_relation"] = "child"
            facts["applicant_identity"] = "student"
        if not facts.get("account_region") and state.market in MARKET_LABEL:
            facts["account_region"] = state.market
            basis["account_region"] = "沿用本次开户咨询地区"
        region = destination_market or facts.get("account_region")
        if (family_mode and region and family_context.get("relationship_market") == region
                and not destination_match):
            region = None
        if region is None and family_mode:
            destination_country = re.search(r"(?:去|赴|到|前往|转去)\s*(英国|美国|香港)", request.text)
            if destination_country:
                region = {"英国": "UK", "美国": "US", "香港": "CN-HK"}[destination_country[1]]
            elif state.market in MARKET_LABEL and state.market != family_context.get("relationship_market"):
                region = state.market
            elif re.search(r"英国", request.text):
                region = "UK"
            elif re.search(r"美国", request.text):
                region = "US"
            elif re.search(r"香港", request.text) and not family_context.get("relationship_market"):
                region = "CN-HK"
            else:
                region = "UK"
                facts["account_region"] = region
                basis["account_region"] = "留学家庭场景暂按英国理解；请纠正目的地"
        if region is None and facts.get("destination_country") in MARKET_LABEL:
            # Rendering default for this turn only.  Kept out of facts/basis so
            # the study destination never becomes a persisted account region.
            region = facts["destination_country"]
        region_assumed = False
        if region not in MARKET_LABEL and family_mode:
            region = "UK"
            facts["account_region"] = region
            basis["account_region"] = "家庭留学安排暂按英国核对；可纠正"
        if region not in MARKET_LABEL:
            if "account_region" in request.asked_fields:
                # The region follow-up is already open and the user asked a new
                # account question instead of answering it. Stop blocking on
                # the region: answer under a stated Hong Kong default; the user
                # can redirect to UK/US in the next turn.
                region = "CN-HK"
                region_assumed = True
                facts["account_region"] = region
                basis["account_region"] = "未提及地区，默认按香港回答"
            else:
                return needs_input(task, "bank_account",
                                   summary="不同地区汇丰的开户条件差别很大，先告诉我想在哪个地区开户"
                                           "（香港、英国或美国），我再按当地规则回答。",
                                   missing=["account_region"], facts=facts, basis=basis,
                                   profile_version=request.profile_version)
        if re.search(r"(?:开户|开账户).*(?:必须现在|现在必须|必须先|一定要现在)", request.text):
            return DomainResult(task_id=task.task_id, domain="bank_account", status="answered",
                                answer=_necessity_answer(region), projection="chat_only",
                                evidence_refs=["account-necessity:no-prerequisite"])
        if facts.get("applicant_age_band") == "under_18":
            return DomainResult(
                task_id=task.task_id, domain="bank_account", status="needs_verification",
                answer=AnswerBody(summary="未成年人开户需按具体年龄、地区和账户核对，不能直接按成人账户办理。",
                                  steps=["请提供实际年龄，并向银行核对监护人陪同或同意、身份证明及可用申请渠道。"]),
                # An explicit refresh must replace the obsolete adult plan with
                # this qualification warning. Chat-only results are deliberately
                # excluded from durable advice, leaving refresh stuck forever.
                projection="advice" if request.refresh_mode else "chat_only",
                observed_facts=facts, observed_basis=basis,
                payload={"account_region": region,
                         "applicant_identity": facts.get("applicant_identity"),
                         "applicant_age_band": "under_18"})
        broad_opening = bool(re.fullmatch(
            r"(?:开户地区[:：]?\s*)?(?:英国|美国|香港)(?:开户|账户)?",
            request.text.strip()))
        if not facts.get("applicant_identity") and broad_opening:
            return needs_input(task, "bank_account", summary="开户方案还取决于实际开户人。",
                               missing=["applicant_identity"], facts=facts, basis=basis,
                               profile_version=request.profile_version)
        try:
            catalog = await self.repository.read(region)
        except BankAccountRepositoryError:
            catalog = {"read_id": "unavailable", "products": [], "routes": [], "rules": [],
                       "documents": [], "fees": [], "relations": []}
        if not catalog["products"]:
            if not family_mode:
                return DomainResult(task_id=task.task_id, domain="bank_account",
                                    status="cannot_answer", answer=AnswerBody(
                                        summary=f"暂时没有可核对的{MARKET_LABEL[region]}汇丰开户资料。"),
                                    observed_facts=facts, observed_basis=basis,
                                    notices=[*notices, Notice(code="hsbc_account_missing",
                                                               message="汇丰开户知识未就绪。")])
            catalog = {"read_id": "unavailable", "products": [], "routes": [], "rules": [],
                       "documents": [], "fees": [], "relations": []}
        if family_mode:
            scenario = scenario or family_context.get("scenario") or "family_setup"
            if (family_context.get("relationship_market") and destination_match is None
                    and re.search(r"客户经理|私行|私人银行", request.text)):
                scenario = _family_scenario(request.text) or scenario
            scenario_answer = _family_scenario_answer(
                scenario=scenario, text=request.text, region=region, facts=facts,
                context=family_context, catalog_available=bool(catalog["products"]))
            if scenario_answer is not None:
                plan_stage = ("handoff_ready" if scenario == "handoff" else
                              "clarifying_location" if not facts.get("application_location") else
                              scenario)
                followup = ([_question("application_location", request.profile_version)]
                            if family_mode and not facts.get("application_location") and scenario in {
                                "family_setup", "timeline", "minor", "eligibility_documents", "fees", "handoff"}
                            else [])
                next_state = reduce_account_state(
                    state, market=region, act="ask_documents", mode="procedural",
                    named_product=None, shown_product_ids=[], candidate_product_ids=[],
                    unresolved_fields=["application_location"] if followup else [])
                observed_facts = {key: value for key, value in facts.items() if key != "account_product"}
                opening_plan = None
                if scenario == "handoff":
                    previous_plan = (raw_state.get("opening_plans") or {}).get(region, {})
                    opening_plan, scenario_answer = build_opening_plan(
                        profile_id=request.profile_id, market=region, facts=facts,
                        context=family_context, results=[], text=request.text,
                        previous=previous_plan)
                stored_plans = dict(raw_state.get("opening_plans") or {})
                if opening_plan:
                    stored_plans[region] = opening_plan
                payload = {"family_mode": True, "family_scenario": scenario,
                           "account_region": region,
                           "conversation_state": {**pack_account_state(raw_state, next_state),
                                                  "profile_id": request.profile_id,
                                                  "family_context": family_context,
                                                  "opening_plans": stored_plans}}
                if opening_plan:
                    payload["opening_plan"] = opening_plan
                return DomainResult(
                    task_id=task.task_id, domain="bank_account",
                    status="needs_input" if followup else "answered", answer=scenario_answer,
                    missing_fields=["application_location"] if followup else [],
                    follow_up_questions=followup,
                    next_actions=["按回答中的核对清单确认目的地银行的个案要求。"],
                    notices=notices, observed_facts=observed_facts,
                    observed_basis={key: basis[key] for key in observed_facts if key in basis},
                    payload=payload)
        explicit_named_ids = normalize_products(request.text, catalog["products"])
        explicit_named = explicit_named_ids[0] if explicit_named_ids else None
        model_named = normalize_product(facts.get("account_product"), catalog["products"])
        named_product = explicit_named or model_named
        act = dialogue_act_from_capabilities(
            task.actions, request.text, named_product=named_product, state=state)
        if act == "ask_necessity":
            # A direct necessity question must not be expanded into the catalog
            # just because it contains the phrase “开户”.
            labels = ["account_necessity"]
        # Subintent rules are deliberately redundant with dialogue-act rules.
        # If a named product is present, do not let the generic focus act erase
        # the actual question being asked about that product.
        if act == "focus_product":
            for label, specific_act in (
                ("application_issue", "troubleshoot"),
                ("fees_balance", "ask_fees"),
                ("eligibility", "ask_eligibility"),
            ):
                if label in labels:
                    act = specific_act
                    break
        mode = answer_mode_for(act)
        requested_goal = infer_goal(request.text, state.goal)
        if act in {"discover_options", "focus_product", "request_alternatives", "ask_catalog_scope",
                   "compare_products", "confirm_selection", "reject_product"}:
            labels = [label for label in labels if label != "documents_process"]
            if "product_selection" not in labels:
                labels.append("product_selection")
        referent = named_product
        if referent is None and act in {"confirm_selection", "ask_process", "ask_documents",
                                       "ask_eligibility", "ask_fees", "troubleshoot"}:
            referent = state.selected_product_id or state.focused_product_id
        if referent and len(explicit_named_ids) <= 1:
            facts["account_product"] = referent
        ambiguous_reference = (
            referent is None and len(state.presented_product_ids) > 1 and
            (act == "confirm_selection" or (
                act in {"ask_process", "ask_documents", "ask_eligibility", "ask_fees", "troubleshoot"}
                and re.search(r"这个|该账户|它(?:的|怎么|能|要|有)", request.text)
            ))
        )
        excluded = exclusion_ids(state, act)
        if named_product and act in {"focus_product", "confirm_selection", "ask_process",
                                    "ask_documents", "ask_eligibility", "ask_fees"}:
            excluded.discard(named_product)
        exhausted = False
        if act == "ask_necessity":
            selected = []
        elif ambiguous_reference:
            by_id = {row["account_id"]: row for row in catalog["products"]}
            selected = [by_id[account_id] for account_id in state.presented_product_ids
                        if account_id in by_id]
        elif act == "compare_products" and len(explicit_named_ids) >= 2:
            by_id = {row["account_id"]: row for row in catalog["products"]}
            selected = [by_id[account_id] for account_id in explicit_named_ids if account_id in by_id]
        elif act == "discover_options" and len(catalog["products"]) > 1:
            discovery = _discovery_products(catalog["products"], requested_goal)
            selected = product_order(discovery, facts, request.text, labels,
                                     limit=len(discovery)) if discovery else []
        elif act == "ask_catalog_scope":
            selected = list(catalog["products"])
        else:
            selected = product_order(catalog["products"], facts, request.text, labels,
                                     exclude_ids=excluded,
                                     limit=3 if act in {"request_alternatives", "reject_product"} else 2)
        if not selected and act != "ask_necessity":
            exhausted = True
            selected = list(catalog["products"])
        assessments = [assess_product(row, catalog, facts) for row in selected]
        for item in assessments:
            account_id = item["product"]["account_id"]
            stored_relationships = [row for row in catalog.get("relations", [])
                                    if row["source_account_id"] == account_id]
            item["relationships"] = [*stored_relationships, *product_relationships(account_id)]
        broad_selection = "product_selection" in labels and referent is None
        selection_limited = broad_selection and len(catalog["products"]) < 2
        if act == "ask_necessity":
            verified = _necessity_answer(region)
        elif ambiguous_reference:
            verified = _ambiguous_reference_answer(selected)
        elif act == "discover_options" and len(catalog["products"]) > 1:
            verified = _discovery_answer(assessments, region,
                                         total=len(catalog["products"]), goal=requested_goal,
                                         facts=facts)
        elif act in {"request_alternatives", "ask_catalog_scope", "reject_product"}:
            verified = _catalog_answer(selected, region, act,
                                       total=len(catalog["products"]), exhausted=exhausted)
        elif act == "compare_products" and len(assessments) >= 2:
            verified = _comparison_answer(assessments, region, facts)
        elif act in {"focus_product", "confirm_selection"} and len(assessments) == 1:
            verified = _focus_answer(assessments[0], region,
                                     confirmed=act == "confirm_selection")
        else:
            verified = build_answer(assessments, labels, region, request.text, facts,
                                    selection_limited=selection_limited)
        if region_assumed:
            assumed_note = ("本回答默认按香港汇丰的规则整理；如果您要开的是英国或美国的账户，"
                            "直接告诉我地区即可。")
            verified = AnswerBody(
                summary=f"（先按香港回答）{verified.summary}",
                steps=[assumed_note, *verified.steps],
                materials=verified.materials, limitations=verified.limitations)
        income_note = _premier_income_note(region, assessments, request.text, facts)
        if income_note and income_note not in verified.steps:
            verified = AnswerBody(
                summary=verified.summary,
                steps=[*verified.steps, income_note],
                materials=verified.materials, limitations=verified.limitations)
        if re.search(r"首选|推荐|怎么选|适合|比较|费用|月费|多少钱", request.text):
            family_plan = family_plan and act not in {"discover_options", "compare_products", "ask_fees"}
        account_ids = [item["product"]["source_account_id"] for item in assessments]
        rules = [row for row in catalog.get("rules", []) if row["account_id"] in account_ids]
        grouped = {}
        for rule in rules:
            grouped.setdefault((rule["account_id"], rule.get("rule_purpose"),
                                rule.get("route_id"), rule.get("group_code")), []).append(rule)
        grouped_results = []
        for (account_id, purpose, route_id, group_code), group_rules in grouped.items():
            evaluated = [evaluate_rule(rule, facts) for rule in group_rules]
            if purpose == "fee_waiver":
                outcome = "meets_published_criteria" if "true" in evaluated else (
                    "needs_input" if "unknown" in evaluated else "does_not_meet_published_criteria")
            elif group_code not in (None, "", "required"):
                outcome = ("does_not_meet_published_criteria" if "false" in evaluated else
                           "needs_input" if "unknown" in evaluated else
                           "requires_bank_confirmation" if "bank_confirmation" in evaluated else
                           "meets_published_criteria")
            else:
                outcome = ("does_not_meet_published_criteria" if "false" in evaluated else
                           "needs_input" if "unknown" in evaluated else
                           "requires_bank_confirmation" if "bank_confirmation" in evaluated else
                           "meets_published_criteria")
            grouped_results.append({"account_id": account_id, "purpose": purpose,
                                    "route_id": route_id, "group_code": group_code,
                                    "status": outcome, "rule_ids": [r["rule_id"] for r in group_rules]})
        for item in assessments:
            source_id = item["product"]["source_account_id"]
            eligible = [result for result in grouped_results
                        if result["account_id"] == source_id and result["purpose"] == "application"]
            required_rule_groups = [group for group in eligible
                                    if group["group_code"] in (None, "", "required")]
            alternatives = [group for group in eligible
                           if group["group_code"] not in (None, "", "required")]
            if alternatives:
                item["decision_status"] = ("meets_published_criteria" if any(
                    group["status"] == "meets_published_criteria" for group in alternatives) else
                    "needs_input" if any(group["status"] == "needs_input" for group in alternatives) else
                    "requires_bank_confirmation" if any(
                        group["status"] == "requires_bank_confirmation" for group in alternatives) else
                    "does_not_meet_published_criteria")
            elif required_rule_groups:
                item["decision_status"] = required_rule_groups[0]["status"]
            else:
                item["decision_status"] = item["status"]
            item["decision_groups"] = eligible
        decision_paths = []
        if family_plan:
            origin_market = family_context.get("relationship_market")
            # Only retrieve cross-border family routes when the stated relationship
            # market is distinct from the destination; never treat it as eligibility.
            if origin_market == region:
                origin_market = None
            try:
                decision_paths = await self.repository.read_decision_paths(
                    region, origin_market=origin_market,
                    applicant_location=facts.get("application_location"))
            except (BankAccountRepositoryError, AttributeError):
                notices.append(Notice(code="account_decision_path_unavailable",
                                      message="家庭开户路径规则暂不可用，已保留需银行确认的安全流程。"))
        decision_results = [evaluate_decision_path(path, facts) for path in decision_paths]

        source_extracts = []
        if family_plan:
            source_ids = [item["product"]["account_id"] for item in assessments]
            if "香港" in family_context.get("parent_relationship_statement", ""):
                source_ids.append("hsbc_hk_premier")
            try:
                source_extracts = await self.repository.read_source_chunks(
                    source_ids, query=f"{request.text} 家庭权益 出发前 海外开户 材料", limit=6)
            except Exception:
                pass
        if family_plan:
            if act not in {"ask_fees", "compare_products", "confirm_selection"}:
                labels = list(dict.fromkeys([*labels, "documents_process"]))
                scenario_answer = _family_scenario_answer(
                    scenario=scenario, text=request.text, region=region, facts=facts,
                    context=family_context, catalog_available=bool(catalog["products"]))
                if scenario_answer is not None:
                    verified = scenario_answer
                location_label = {"CN": "中国内地", "CN-HK": "香港", "UK": "英国", "US": "美国"}.get(
                    facts.get("application_location"))
                location_step = (f"孩子目前在{location_label}，下一步核对从这里申请的渠道是否开放，"
                                 "以及身份核验、收卡和激活哪些能在出发前完成。" if location_label else
                                 "先确认孩子目前的申请所在地，再核对可用的境外申请渠道，以及哪些步骤需要到当地完成。")
                verified = AnswerBody(
                    summary=f"先围绕孩子去{MARKET_LABEL[region]}后的收付款和出发前准备来安排，账户资格与办理渠道要分别核对。",
                    steps=[
                        "现有银行关系可作为查询家庭跨境开户服务的线索；是否适用尚未由银行确认。",
                        location_step,
                        "先整理孩子的身份证明、现有常住地址证明、留学地区及出发月份；不同申请渠道接受的文件可能不同，先不要假定某种替代材料一定有效。",
                        "请客户经理确认孩子适用的英国账户及申请渠道、内地申请是否开放、本人是否需到场，以及收卡和激活能否在出发前完成。这里还没有替您预约或提交申请。",
                    ], limitations=["家庭服务资格、申请材料及办理时间尚待银行按孩子个案核实。"])
                if re.search(r"准备|材料|带什么", request.text):
                    verified = AnswerBody(
                        summary="可以先把给客户经理的资料整理好，同时请他确认从孩子当前所在地办理的具体路径。",
                        steps=[location_step,
                               "先备好孩子的身份证明和现有常住地址证明；是否接受当前文件、是否需要补件，请客户经理按实际渠道确认，先不要为了开户办理未经确认的额外证明。",
                               "把孩子的留学地区、计划出发时间和希望出发前完成的事项写清楚，并说明希望核对您现有银行关系下的家庭服务。",
                               "请客户经理明确回复：孩子适用哪个账户、能从哪里申请、是否需要本人到场、材料清单，以及收卡和激活的安排。这样可以减少孩子落地后反复补办。"],
                        limitations=["这是准备及交接清单，尚未提交申请或预约；最终材料以确认的渠道为准。"])
        opening_plan = {}
        if family_plan:
            previous_plan = (raw_state.get("opening_plans") or {}).get(region, {})
            opening_plan, verified = build_opening_plan(
                profile_id=request.profile_id, market=region, facts=facts,
                context=family_context, results=decision_results, text=request.text,
                previous=previous_plan)
        body = verified
        model_version = "account-decision-plan-1" if family_plan else "account-seed-rules-1"
        model_failure = None
        deterministic_dialogue = (
            act == "ask_necessity" or ambiguous_reference or act == "compare_products" or
            (act == "discover_options" and len(catalog["products"]) > 1) or
            act in {"request_alternatives", "ask_catalog_scope", "reject_product",
                    "focus_product", "confirm_selection"}
        )
        if self.model and not deterministic_dialogue and not family_plan:
            try:
                # Official-page extracts: the structured rules stay the source of
                # truth for deterministic checks, but pages carry more detail
                # (eligibility paths, documents, offers) than the rule rows.
                # Relevant chunks are supplied to the writer as quotable material;
                # their numbers/wording count as supported input.
                try:
                    source_extracts = source_extracts or await self.repository.read_source_chunks(
                        [item["product"]["account_id"] for item in assessments],
                        query=f"{request.text} {named_product or ''}",
                    )
                except Exception:
                    source_extracts = []
                if source_extracts:
                    notices.append(Notice(
                        code="account_source_extracts",
                        message=f"已检索 {len(source_extracts)} 段汇丰官方页面原文作为补充依据。"))
                extracts_text = " ".join(
                    f"{chunk.get('heading') or ''} {chunk['content']}" for chunk in source_extracts)
                verified_payload = {"facts": facts, "subintents": labels,
                                    "decision_paths": decision_results,
                                    "family_context": family_context,
                                    "profile_id": request.profile_id,
                                    "previous_answer": request.session_last_assistant_text,
                                    "pending_questions": request.session_pending_questions,
                                    "assessments": _writer_assessments(assessments, labels),
                                    "selection_context": {
                                        "broad_request": broad_selection,
                                        "catalog_product_count": len(catalog["products"]),
                                        "comparison_supported": not selection_limited,
                                        "region_assumed": region_assumed,
                                    },
                                    "source_extracts": [
                                        {"heading": chunk.get("heading"),
                                         "content": chunk["content"],
                                         "source_url": chunk.get("source_url"),
                                         "doc_id": chunk.get("doc_id")}
                                        for chunk in source_extracts],
                                    "verified_answer": verified.model_dump(mode="json")}
                verified_json = json.dumps(verified_payload, ensure_ascii=False, default=str)
                prompt = self.renderer.render(
                    "write_bank_answer_v3.jinja2", user_text=request.text,
                    verified_json=verified_json,
                    source_extracts_json=json.dumps(
                        verified_payload["source_extracts"], ensure_ascii=False)
                    if source_extracts else "",
                    profile_name=request.profile_name, profile_role=request.profile_role,
                )
                written = await self.model.parse(prompt=prompt, output_type=WrittenAnswer,
                                                 operation="write_bank_answer")
                candidate = AnswerBody(summary=written.summary,
                                       steps=list(written.steps) or list(verified.steps),
                                       materials=verified.materials, limitations=verified.limitations)
                # Rule rows and fee tables fed to the writer may contain numbers
                # (age bands, tenors, thresholds) that the deterministic answer
                # spells differently. Restating them is grounded, not a new claim;
                # numbers absent from the fed data are still rejected.
                validate_written_answer(candidate, verified,
                                        supported_text=f"{verified_json} {extracts_text}",
                                        profile_name=request.profile_name)
                _validate_writer_scope(
                    candidate, labels, selection_limited=selection_limited,
                    allowed_product_names=[row["product_name"] for row in catalog["products"]],
                    supported_text=f"{extracts_text} {_english_content(verified_payload)}",
                    query_text=request.text,
                    family_relationship=bool(family_context.get("parent_relationship_statement")),
                )
                body = candidate
                model_version = f"{self.model.model_name}+{PROMPT_VERSION}"
            except Exception as exc:
                model_failure = type(exc).__name__
                notices.append(Notice(code="bank_llm_fallback",
                                      message="模型未完成开户表述，已使用核验资料生成回答。"))
        citations, claims = evidence(assessments, catalog["read_id"])
        for chunk in source_extracts:
            if not chunk.get("verified_at"):
                continue
            citations.append(Citation(
                citation_id=f"C{len(citations) + 1}", record_id=chunk["doc_id"],
                revision_id=catalog["read_id"], evidence_id=chunk["chunk_id"],
                source_name=chunk.get("title") or "汇丰官方开户资料",
                source_url=chunk["source_url"], quote=chunk["content"],
                verified_at=datetime.fromisoformat(chunk["verified_at"]).replace(tzinfo=timezone.utc),
                synthetic=False))
        missing_questions = _missing_account_questions(assessments)
        if family_plan and act not in {"ask_fees", "compare_products", "confirm_selection"}:
            missing_questions = (["application_location"]
                                 if not facts.get("application_location") else [])
        # A fee question can be answered from the published fee rows even if
        # applicant eligibility is still unknown. Do not start an application
        # interview when the user only asked about account charges.
        if labels == ["fees_balance"]:
            missing_questions = []
        followups = ([_question(missing_questions[0], request.profile_version)]
                     if missing_questions else [])
        if "documents_process" in labels and not followups and region == "CN-HK" and \
                re.search(r"app|线上|在线", request.text, re.I) and \
                not facts.get("identity_document_type"):
            followups = [_question("identity_document_type", request.profile_version)]
        unresolved = list(missing_questions)
        state.goal = infer_goal(request.text, state.goal)
        next_state = reduce_account_state(
            state, market=region, act=act, mode=mode, named_product=referent,
            shown_product_ids=[row["account_id"] for row in selected],
            candidate_product_ids=[row["account_id"] for row in catalog["products"]],
            unresolved_fields=unresolved,
        )
        next_state.goal = infer_goal(request.text, next_state.goal)
        # account_product is conversational focus, not a durable Profile fact.
        observed_facts = {key: value for key, value in facts.items() if key != "account_product"}
        observed_basis = {key: value for key, value in basis.items() if key != "account_product"}
        evidence_refs = [item["product"]["account_id"] for item in assessments]
        # Necessity is a judgement, not a product quote. An empty ref list would
        # be downgraded to needs_verification and stamped with card-catalog copy.
        if act == "ask_necessity":
            evidence_refs.append("account-necessity:no-prerequisite")
        return DomainResult(task_id=task.task_id, domain="bank_account",
                            status="needs_input" if followups else "answered",
                            answer=body, claims=claims, citations=citations,
                            missing_fields=missing_questions,
                            evidence_refs=evidence_refs,
                            follow_up_questions=followups,
                            next_actions=["查看上述汇丰官网来源，按适用渠道核对最新要求。"],
                            notices=notices, observed_facts=observed_facts,
                            observed_basis=observed_basis,
                            payload={"subintents": labels, "account_region": region,
                                     "dialogue_act": act, "answer_mode": mode,
                                     "decision_paths": decision_results,
                                     "ambiguous_reference": ambiguous_reference,
                                     "conversation_state": {
                                         **pack_account_state(raw_state, next_state),
                                         "profile_id": request.profile_id,
                                         "family_context": family_context,
                                         "opening_plans": {**(raw_state.get("opening_plans") or {}),
                                                           **({region: opening_plan} if opening_plan else {})}},
                                     "opening_plan": opening_plan,
                                     "candidate_results": assessments,
                                     "catalog_product_count": len(catalog["products"]),
                                     "selection_coverage":
                                     "limited" if selection_limited else "sufficient_or_specific",
                                     "knowledge_source": catalog["read_id"],
                                     "classification_prompt_version":
                                     self.classifier.prompt_version if self.classifier else "rules",
                                     "model_failure": model_failure},
                            knowledge_read_id=catalog["read_id"], model_version=model_version)
