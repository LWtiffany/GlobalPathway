"""Grounded customer-service copy assembled before optional model rewriting."""

from __future__ import annotations

import re
from datetime import datetime, timezone

from agent2.models import AnswerBody, Citation, Claim

from .bank_account_dialogue import relationship_messages

MARKET_LABEL = {"UK": "英国", "US": "美国", "CN-HK": "香港"}

ENGLISH_SENTENCE = re.compile(r"\b(?:[A-Za-z]{2,}\s+){2,}[A-Za-z]{2,}\b")

FRIENDLY_CONDITION = {
    "guardian_present": "未成年申请人办理时是否有家长或监护人陪同",
    "uk_residency_years": "申请人在英国或指定属地的居住年限",
    "qualifying_course_offer": "申请人是否有符合要求的课程录取证明",
    "tax_certificate_or_ssn": "申请人是否有 SSN 或可接受的税务证明",
}


def _friendly_condition(rule: dict) -> str:
    if rule["field_code"] == "applicant_age" and rule.get("value_numeric") is not None:
        return f"申请人是否已满 {rule['value_numeric']:g} 岁"
    if rule["field_code"] == "uk_residency_years" and rule.get("value_numeric") is not None:
        return f"申请人是否已在英国或指定属地居住满{rule['value_numeric']:g}年"
    return FRIENDLY_CONDITION.get(
        rule["field_code"], _chinese_or(rule["description"], "银行要求的相关开户条件"))


def _chinese_or(value: str | None, fallback: str) -> str:
    if not value or ENGLISH_SENTENCE.search(value):
        return fallback
    return value


def _product_description(product: dict) -> str:
    segment = "面向 Premier 客户的" if product.get("customer_segment") == "premier" else ""
    kind = {
        "checking": "支票账户", "current": "往来账户", "savings": "储蓄账户",
    }.get(product.get("account_type"), "银行账户")
    return f"{segment}{kind}"


def _route_guidance(route: dict) -> str:
    route_id = route.get("route_id", "").casefold()
    channel = route.get("channel", "").casefold()
    if "us_premier" in route_id:
        return "可以先在汇丰官网填写在线申请；联名申请或不符合在线申请条件时，可能需要汇丰回电协助。"
    if channel in {"online", "web"}:
        return "可以先通过汇丰官网在线申请；是否需要补充资料，以申请页面或银行通知为准。"
    if channel in {"app", "mobile", "mobile_app"}:
        return "可以通过汇丰手机银行 App 申请；是否适用这条渠道，要看身份文件、年龄和所在地等条件。"
    if channel == "branch":
        return "需要前往汇丰网点办理；建议先确认是否需要预约，并按要求携带材料。"
    return _chinese_or(route.get("route_note"), "请通过汇丰官方渠道确认具体办理方式。")


def _document_label(row: dict) -> str:
    labels = {
        "identity": "身份证明（如护照或银行接受的政府签发证件）",
        "guardian_identity": "家长或监护人的身份证明",
        "address": "常住地址证明（如银行账单或水电账单）",
        "education": "录取通知书或在读证明",
        "enrollment": "录取通知书或在读证明",
        "course_offer": "符合要求的课程录取证明",
        "tax": "税号及税务信息",
        "tax_information": "税号及税务信息",
        "immigration": "签证或其他移民身份证明",
    }
    return labels.get(row.get("document_type"),
                      _chinese_or(row.get("description"), "银行要求的其他证明材料"))


def _status(status: str) -> str:
    return {
        "meets_published_criteria": "已提供条件符合收录的公开要求，仍需银行审核",
        "does_not_meet_published_criteria": "有已知条件不符合公开要求",
        "needs_input": "还缺少决定资格的条件",
        "requires_bank_confirmation": "该渠道仍需向汇丰确认",
    }[status]


def build_answer(assessments: list[dict], labels: list[str], region: str,
                 user_text: str, facts: dict, *, selection_limited: bool = False) -> AnswerBody:
    primary = assessments[0]
    name = primary["product"]["product_name"]
    steps: list[str] = []
    if "account_necessity" in labels:
        steps.append("是否现在开当地账户，取决于学校收款方式及到达后的日常收付款安排；可以先了解汇丰当地的申请路径。")
    if "product_selection" in labels:
        if selection_limited:
            product = primary["product"]
            steps.append(
                f"目前资料库在{MARKET_LABEL[region]}只收录了 {product['product_name']}，"
                "候选不足，不能据此判断它就是最适合您的账户。"
            )
            steps.append(f"已收录产品：{product['product_name']}，属于{_product_description(product)}。")
            unknown = [_friendly_condition(entry["rule"])
                       for entry in primary["rule_results"]
                       if entry["result"] == "unknown"]
            if unknown:
                steps.append("如要判断这个产品是否适用，还需要确认：" + "；".join(unknown) + "。")
        else:
            for item in assessments:
                if item["status"] == "needs_input":
                    unknown = [_friendly_condition(entry["rule"]) for entry in item["rule_results"]
                               if entry["result"] == "unknown"]
                    detail = "；".join(unknown) if unknown else "影响适用性的开户条件"
                    steps.append(f"{item['product']['product_name']}：属于"
                                 f"{_product_description(item['product'])}；是否适合还需确认：{detail}。")
                else:
                    steps.append(f"{item['product']['product_name']}：{_status(item['status'])}，"
                                 f"属于{_product_description(item['product'])}。")
                failed = [_friendly_condition(entry["rule"]) for entry in item["rule_results"]
                          if entry["result"] == "false"]
                if failed:
                    steps.append("不符合的已公布条件：" + "；".join(failed) + "。")
                steps.extend(relationship_messages(item.get("relationships", [])))
    if "eligibility" in labels:
        for item in assessments:
            steps.append(f"{item['product']['product_name']}：{_status(item['status'])}。")
            for result in item["rule_results"]:
                if result["result"] == "false":
                    steps.append(f"目前不符合：{_friendly_condition(result['rule'])}。")
                elif result["result"] == "unknown":
                    steps.append(f"还需核对：{_friendly_condition(result['rule'])}。")
            if item["status"] == "meets_published_criteria":
                # The same logical condition can be stored as several rule rows;
                # state each distinct requirement once.
                conditions = list(dict.fromkeys(
                    _friendly_condition(result["rule"]) for result in item["rule_results"]))
                steps.append("已收录的公开条件：" + "；".join(conditions) + "。")
            for route in item["routes"]:
                if route["status"] != "meets_published_criteria":
                    steps.append(f"{route['route']['route_name']}：{_status(route['status'])}。")
    if "documents_process" in labels:
        channel = "app" if re.search(r"app|线上|在线|网上", user_text, re.I) else \
            "branch" if re.search(r"网点|分行", user_text) else None
        routes = [entry for entry in primary["routes"]
                  if channel is None or entry["route"]["channel"] == channel]
        if not routes:
            routes = primary["routes"]
        prerequisites = [_friendly_condition(entry["rule"]) for entry in primary["rule_results"]
                         if entry["result"] in {"unknown", "bank_confirmation"}]
        if prerequisites:
            steps.append("申请前先确认：" + "；".join(prerequisites) + "。")
        for entry in routes[:2]:
            route = entry["route"]
            steps.append(f"申请方式：{_route_guidance(route)}")
            if entry["status"] == "does_not_meet_published_criteria":
                steps.append("根据已提供的信息，这条申请渠道不适用；可了解其他渠道。")
        docs = [row for row in primary["documents"]
                if row["route_id"] is None or
                any(row["route_id"] == entry["route"]["route_id"] for entry in routes)]
        if docs:
            required = [_document_label(row) for row in docs if row["requirement_level"] == "required"]
            possible = [_document_label(row) for row in docs if row["requirement_level"] != "required"]
            if required:
                steps.append("需要准备：" + "；".join(dict.fromkeys(required)) + "。")
            if possible:
                steps.append("银行还可能要求：" + "；".join(dict.fromkeys(possible)) + "。")
        steps.append("提交申请后，请留意申请页面和银行通知；如需补充资料，按通知处理即可。")
    if "fees_balance" in labels:
        for fee in primary["fees"]:
            if fee["fee_id"] in {"hk_one_non_hkid_below_balance", "fee_hk_one_nonhkid"} and \
                    facts.get("has_hk_identity_card") is True:
                steps.append("香港身份证持有人不适用这项 HSBC One 低结余费。")
                continue
            kind = {
                "monthly_account": "每月账户费", "monthly_fee": "每月账户费",
                "below_balance": "每月低结余费", "below_balance_fee": "每月低结余费",
                "minimum_relationship_balance": "费用豁免关系余额",
                "foreign_transaction_fee": "外币交易费",
                "overseas_atm_fee": "境外提款费",
            }.get(fee["fee_type"], "账户费用")
            if fee.get("amount") is not None:
                amount = f"{fee['amount']:g}" if hasattr(fee["amount"], "__format__") else str(fee["amount"])
                line = f"{kind}：{amount} {fee.get('currency') or ''}。"
            elif fee.get("rate") is not None:
                line = f"{kind}：费率 {fee['rate']}。"
            else:
                line = f"{kind}：请查看官网当前收费标准。"
            if fee["condition_note"]:
                line += "适用条件：" + _chinese_or(
                    fee["condition_note"], "以汇丰官网公布的产品条件为准") + "。"
            if fee["waiver_note"]:
                line += "豁免说明：" + _chinese_or(
                    fee["waiver_note"], "满足汇丰公布的相应条件时可免收") + "。"
            steps.append(line)
            if fee["fee_id"] in {"hk_one_non_hkid_below_balance", "fee_hk_one_nonhkid"} and \
                    facts.get("has_hk_identity_card") is False:
                steps.append("您说没有香港身份证；还需看开户日期及三个月平均总关系结余，才能判断是否收费。")
        if primary["fee_waivers"] and not (
            primary["product"]["account_id"] == "hsbc_hk_one" and
            facts.get("has_hk_identity_card") is True
        ):
            steps.append("另有公开的免月费路径：" + "；".join(
                _chinese_or(row["description"], "满足汇丰公布的相应关系条件")
                for row in primary["fee_waivers"]) + "。")
        if re.search(r"最低存款|开户门槛|最低要存|要存.*钱", user_text):
            steps.append("上述余额用于费用判断；现有资料没有据此可断定的开户最低存款。")
    if "application_issue" in labels:
        steps.append("已收录资料没有这个具体报错的处理结论；请核对申请页面提示，并通过对应的汇丰官方申请渠道查询或补交材料。")
    if not steps:
        steps.append(f"{name} 属于{_product_description(primary['product'])}。")
    # Multi-label requests walk several branches that can emit the same line
    # (product status, channel guidance). Present each distinct line once.
    steps = list(dict.fromkeys(steps))
    summary = (
        f"目前收录的{MARKET_LABEL[region]}汇丰账户不足以完成产品比较，"
        "先说明现有候选和还需确认的信息。"
        if selection_limited else
        (f"如果您准备在{MARKET_LABEL[region]}开立汇丰 {name}，可以按下面的方式准备。"
         if "documents_process" in labels else
         f"关于{MARKET_LABEL[region]}汇丰 {name}，下面是与您问题最相关的信息。")
    )
    return AnswerBody(
        summary=summary,
        steps=steps,
        limitations=["这里只提供开户咨询；实际申请条件和审批结果以汇丰办理时的信息为准。",
                     "资料来源为已核验的汇丰官网页面，政策变化时应重新核对。"],
    )


def evidence(assessments: list[dict], revision_id: str) -> tuple[list[Citation], list[Claim]]:
    rows: list[tuple[str, str, str]] = []
    for item in assessments:
        product = item["product"]
        rows.append((product["official_url"], product["verified_at"], product["summary"]))
        for entry in item["rule_results"]:
            row = entry["rule"]
            rows.append((row["source_url"], row["verified_at"], row["description"]))
        for entry in item["routes"]:
            row = entry["route"]
            rows.append((row["source_url"], row["verified_at"], row["route_note"]))
        for category in ("documents", "fees", "features", "fee_waivers"):
            for row in item[category]:
                quote = row.get("description") or row.get("condition_note") or \
                    row.get("waiver_note") or "汇丰账户费用资料"
                rows.append((row["source_url"], row["verified_at"], quote))
        for row in item.get("relationships", []):
            if row.get("source_url") and row.get("verified_at"):
                rows.append((row["source_url"], row["verified_at"], row["message"]))
    citations: list[Citation] = []
    for url, verified, quote in rows:
        if any(item.source_url == url for item in citations):
            continue
        citations.append(Citation(
            citation_id=f"C{len(citations) + 1}", record_id="hsbc-account-016",
            revision_id=revision_id, evidence_id=url, source_name="汇丰官方账户资料",
            source_url=url, quote=quote, verified_at=datetime.fromisoformat(verified).replace(
                tzinfo=timezone.utc), synthetic=False,
        ))
    claims = [Claim(claim_id=f"account-{index}", text=quote, risk="high",
                    citation_ids=[next(c.citation_id for c in citations if c.source_url == url)])
              for index, (url, _verified, quote) in enumerate(rows, 1)]
    return citations, claims
