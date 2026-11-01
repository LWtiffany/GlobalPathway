"""HSBC account-opening customer service, grounded in reviewed account rows."""

from __future__ import annotations

import json
import re

from agent2 import clarify
from agent2.llm.account_subintent import AccountSubintentClassifier
from agent2.llm.contracts import StructuredModel
from agent2.llm.prompts import PromptRenderer
from agent2.llm.schemas import WrittenAnswer
from agent2.llm.validation import validate_written_answer
from agent2.models import AnswerBody, FollowUpQuestion, Notice

from ..contracts import DomainResult, DomainTask, OrchestrationInput
from .bank_account_answer import MARKET_LABEL, build_answer, evidence
from .bank_account_dialogue import (answer_mode_for, dialogue_act_from_capabilities,
                                    exclusion_ids, infer_goal, load_account_state,
                                    pack_account_state, product_relationships, reduce_account_state,
                                    relationship_messages)
from .bank_account_engine import (assess_product, normalize_product,
                                   normalize_products, positioning_for, product_order,
                                   PRODUCT_POSITIONING)
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
              "existing_hsbc_hk_customer": bool}
QUESTION_TEXT = {
    "uk_residency_years": "实际开户人在英国或指定属地住了几年？这会影响学生账户资格。",
    "identity_document_type": "实际开户人用香港身份证、护照，还是往来港澳通行证申请？",
    "tax_certificate_or_ssn": "申请人有 SSN，或可提交有效税务证明吗？",
    "qualifying_course_offer": "申请人有符合该账户条件的课程录取证明吗？",
    "guardian_present": "未成年开户人到网点时会有家长或监护人陪同吗？",
}

INCOME_MENTION = re.compile(r"年收入|年薪|税前收入|月收入|\bincome\b", re.I)

# HSBC deposit accounts qualify Premier by relationship balance / assets, never
# by annual income (see 016_hsbc_deposit_accounts_seed.sql: hk_premier_trb,
# us_fee_balance). A user who volunteers income expecting it to unlock Premier
# gets a grounded correction instead of a fabricated income threshold.
PREMIER_INCOME_CLARIFICATION = {
    "CN-HK": "补充一点：香港汇丰 Premier 的资格是按「总关系结余」判定的——最近三个月平均约 100 万港元，"
             "或您已在其他国家/地区持有合资格的汇丰 Premier（International Premier）；"
             "年收入本身不是官网列出的开户门槛。方便的话可以告诉我您大致能维持的存款/投资结余，我再据此判断。",
    "US": "补充一点：美国汇丰 Premier 是按关系条件豁免月费的——合格存款及投资总关系余额达 10 万美元、"
          "每月合资格直接入账达 5,000 美元、持有汇丰美国按揭，或为汇丰私人银行客户，满足其一即可；"
          "年收入本身不是官网列出的门槛。",
    "UK": "补充一点：已收录的英国汇丰账户公开条件里没有年收入门槛；是否需要 Premier 级服务，"
          "以汇丰官网公布的关系条件为准。",
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
                           query_text: str = "") -> None:
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


def _explicit_facts(text: str) -> dict:
    facts = {}
    folded = text.casefold()
    if "香港" in text or "hsbc one" in folded or "汇丰 one" in folded:
        facts["account_region"] = "CN-HK"
    elif "英国" in text or "uk" in folded:
        facts["account_region"] = "UK"
    elif "美国" in text or "premier checking" in folded:
        facts["account_region"] = "US"
    age = re.search(r"(?<!\d)(\d{1,2})\s*岁", text)
    if age:
        facts["applicant_age"] = int(age.group(1))
    if re.search(r"刚到英国|刚落地英国", text):
        facts["uk_residency_years"] = 0
    years = re.search(r"(?:在英国|英国).*?(?:住了|居住了|住满)\s*(\d+)\s*年", text)
    if years:
        facts["uk_residency_years"] = int(years.group(1))
    if re.search(r"孩子|儿子|女儿", text):
        facts["account_applicant_relation"] = "child"
        facts["applicant_identity"] = "student"
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
        # The current turn must beat the previous turn's stored query scope.
        explicit_seed = _explicit_facts(request.text)
        market_hint = explicit_seed.get("account_region") or facts.get("account_region")
        state = load_account_state(raw_state, market_hint)
        labels = _fallback_labels(request.text)
        notices: list[Notice] = []
        if self.classifier and request.input_mode == "free_text":
            try:
                classified = await self.classifier.classify(request.text)
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
        if not facts.get("account_region") and state.market in MARKET_LABEL:
            facts["account_region"] = state.market
            basis["account_region"] = "沿用本次开户咨询地区"
        region = facts.get("account_region")
        if region is None and facts.get("destination_country") in MARKET_LABEL:
            # Rendering default for this turn only.  Kept out of facts/basis so
            # the study destination never becomes a persisted account region.
            region = facts["destination_country"]
        region_assumed = False
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
            return DomainResult(task_id=task.task_id, domain="bank_account",
                                status="cannot_answer", answer=AnswerBody(
                                    summary=f"暂时没有可核对的{MARKET_LABEL[region]}汇丰开户资料。"),
                                observed_facts=facts, observed_basis=basis,
                                notices=[*notices, Notice(code="hsbc_account_missing",
                                                           message="汇丰开户知识未就绪。")])
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
        body = verified
        model_version = "account-seed-rules-1"
        model_failure = None
        deterministic_dialogue = (
            act == "ask_necessity" or ambiguous_reference or act == "compare_products" or
            (act == "discover_options" and len(catalog["products"]) > 1) or
            act in {"request_alternatives", "ask_catalog_scope", "reject_product",
                    "focus_product", "confirm_selection"}
        )
        if self.model and not deterministic_dialogue:
            try:
                # Official-page extracts: the structured rules stay the source of
                # truth for deterministic checks, but pages carry more detail
                # (eligibility paths, documents, offers) than the rule rows.
                # Relevant chunks are supplied to the writer as quotable material;
                # their numbers/wording count as supported input.
                try:
                    source_extracts = await self.repository.read_source_chunks(
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
                                        supported_text=f"{verified_json} {extracts_text}")
                _validate_writer_scope(
                    candidate, labels, selection_limited=selection_limited,
                    allowed_product_names=[row["product_name"] for row in catalog["products"]],
                    supported_text=f"{extracts_text} {_english_content(verified_payload)}",
                    query_text=request.text,
                )
                body = candidate
                model_version = f"{self.model.model_name}+{PROMPT_VERSION}"
            except Exception as exc:
                model_failure = type(exc).__name__
                notices.append(Notice(code="bank_llm_fallback",
                                      message="模型未完成开户表述，已使用核验资料生成回答。"))
        citations, claims = evidence(assessments, catalog["read_id"])
        missing_questions = _missing_account_questions(assessments)
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
        return DomainResult(task_id=task.task_id, domain="bank_account",
                            status="needs_input" if followups else "answered",
                            answer=body, claims=claims, citations=citations,
                            missing_fields=missing_questions,
                            evidence_refs=[item["product"]["account_id"] for item in assessments],
                            follow_up_questions=followups,
                            next_actions=["查看上述汇丰官网来源，按适用渠道核对最新要求。"],
                            notices=notices, observed_facts=observed_facts,
                            observed_basis=observed_basis,
                            payload={"subintents": labels, "account_region": region,
                                     "dialogue_act": act, "answer_mode": mode,
                                     "ambiguous_reference": ambiguous_reference,
                                     "conversation_state": pack_account_state(raw_state, next_state),
                                     "candidate_results": assessments,
                                     "catalog_product_count": len(catalog["products"]),
                                     "selection_coverage":
                                     "limited" if selection_limited else "sufficient_or_specific",
                                     "knowledge_source": catalog["read_id"],
                                     "classification_prompt_version":
                                     self.classifier.prompt_version if self.classifier else "rules",
                                     "model_failure": model_failure},
                            knowledge_read_id=catalog["read_id"], model_version=model_version)
