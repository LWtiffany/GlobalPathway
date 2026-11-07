"""Small deterministic boundary around published account criteria and channels."""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

PRODUCT_ALIASES = {
    "hsbc_uk_bank": ("普通账户", "bank account"),
    "hsbc_uk_student": ("student account", "student bank account", "学生账户"),
    "hsbc_uk_international_student": ("international student", "国际学生"),
    "hsbc_uk_premier": ("premier bank account", "英国卓越"),
    "hsbc_us_premier_checking": ("premier checking", "checking account"),
    "hsbc_us_global_money": ("global money", "多币种账户"),
    "hsbc_us_premier_savings": ("premier savings",),
    "hsbc_us_premier_relationship_savings": ("relationship savings", "关系储蓄"),
    "hsbc_us_new_money_cd": ("new money cd", "新资金定期"),
    "hsbc_us_online_cd": ("online cd", "网上定期"),
    "hsbc_hk_one": ("hsbc one", "汇丰 one", "汇丰one"),
    "hsbc_hk_premier": ("hsbc premier", "汇丰卓越", "premier"),
    "hsbc_hk_children_savings": ("儿童储蓄", "儿童账户"),
    "hsbc_hk_teens": ("hsbc teens", "青少年账户"),
    "hsbc_hk_premier_elite": ("premier elite", "卓越理财尊尚"),
    "hsbc_hk_hkd_savings": ("hong kong dollar savings", "港元储蓄", "港币储蓄"),
    "hsbc_hk_hkd_current": ("hong kong dollar current", "港元往来", "港币往来", "支票账户"),
    "hsbc_hk_time_deposit": ("hsbc time deposit", "time deposit", "定期存款"),
    "hsbc_hk_basic_independence": ("basic banking account with independence", "自主基础银行账户"),
}

# Positioning for personalized recommendations. Every entry is sourced from the
# archived official product page (agent1/migrations/sources/), not invented:
# highlights must be checkable against the stored source extracts.
PRODUCT_POSITIONING = {
    "hsbc_hk_one": {
        "for": "想要一个日常主账户的人（收薪金、付款、多币种都管）",
        "points": [
            "App 开户约 5 分钟、4 步完成（18-75 岁且未持有汇丰账户适用）",
            "支持 12 种主要货币，含美元、英镑、人民币、日元，留学缴费和消费都方便",
            "香港身份证客户无最低结余要求；App 内有支出预算追踪",
        ],
    },
    "hsbc_hk_premier": {
        "for": "资产规模较大，或已在其他国家持有汇丰 Premier 的人",
        "points": [
            "两条资格路径：总关系结余 100 万港元，或 International Premier（他国持有/符合资格）",
            "跨境场景强：全球转账对 Premier 客户免费，多地账户统一管理",
        ],
    },
    "hsbc_hk_children_savings": {
        "for": "未满 18 岁的儿童（家长代办或陪同）",
        "points": [
            "11-17 岁由家长或监护人陪同网点办理；11 岁以下由家长以本人名义代开",
            "港元储蓄账户，无月费负担，适合给孩子起步理财",
        ],
    },
    "hsbc_hk_teens": {
        "for": "11 至 17 岁、持香港永久居民身份证并可由现有汇丰客户家长协助的人",
        "points": [
            "与家长在 App 内约 15 分钟申请，需要香港身份证和香港/内地出生证明",
            "无月费、无最低结余要求，支持 12 种货币；18 岁可沿用账号升级",
        ],
    },
    "hsbc_hk_premier_elite": {
        "for": "已是 Premier 且在香港维持至少 780 万港元总关系结余的人",
        "points": ["属于 Premier 之上的服务层级，并非一般日常账户的入门选择"],
    },
    "hsbc_hk_hkd_savings": {
        "for": "需要随时存取并赚取存款利息的人",
        "points": ["可用 ATM、电话、网上及手机银行管理；新开户通常到网点办理"],
    },
    "hsbc_hk_hkd_current": {
        "for": "需要以港元签发支票及处理日常付款的人",
        "points": ["属于单一港元往来账户，开户需带所需文件到网点"],
    },
    "hsbc_hk_time_deposit": {
        "for": "一段时间内不需动用资金、希望锁定存期的人",
        "points": ["港元定存最低 10,000 港元；提前支取须经银行同意且可能损失利息并收费"],
    },
    "hsbc_hk_basic_independence": {
        "for": "因心智能力受损而需要受托人协助管理日常财务的人",
        "points": ["须以已注册的持久授权书、监护令或法院委任等文件到网点办理"],
    },
    "hsbc_uk_bank": {
        "for": "去英国但未住满 3 年的人（学生账户的替代选择）",
        "points": ["可从英国境外多个地区通过 App 申请，落地前就能开"],
    },
    "hsbc_uk_student": {
        "for": "已在英国或指定属地住满 3 年、就读合资格课程的学生",
        "points": ["学生专属条款；线上申请备好 16 位 UCAS 状态码"],
    },
    "hsbc_us_premier_checking": {
        "for": "在美国生活、希望月费可豁免的人",
        "points": ["月费可由多条资格路径豁免（具体以汇丰官网路径为准）"],
    },
}


def positioning_for(facts: dict) -> str:
    """One-line persona description used to open a recommendation."""
    if facts.get("applicant_age") is not None and facts["applicant_age"] < 18:
        return "为未满 18 岁的孩子安排账户"
    if facts.get("account_applicant_relation") == "child":
        return "为孩子安排账户"
    if facts.get("role") == "student" or facts.get("applicant_identity") == "student":
        return "在读学生"
    return "留学场景"


def normalize_product(value: str | None, products: list[dict]) -> str | None:
    if not value:
        return None
    folded = value.casefold().strip()
    exact = next((row["account_id"] for row in products
                  if folded in (row["account_id"], row["product_name"].casefold())), None)
    if exact:
        return exact
    matches = [(len(alias), row["account_id"]) for row in products
               for alias in PRODUCT_ALIASES.get(row["account_id"], ()) if alias in folded]
    return max(matches)[1] if matches else None


def normalize_products(value: str | None, products: list[dict]) -> list[str]:
    """Return every explicitly named catalog product in textual order.

    ``normalize_product`` intentionally returns one focus product.  A comparison
    such as "One 和 Premier 有什么不同" is different: collapsing both names to
    one product makes a grounded comparison impossible.  Prefer the longest
    alias at each text position and de-duplicate product ids.
    """
    if not value:
        return []
    folded = value.casefold().strip()
    matches: list[tuple[int, int, str]] = []
    for row in products:
        account_id = row["account_id"]
        aliases = {row["product_name"].casefold(), account_id.casefold(),
                   *(alias.casefold() for alias in PRODUCT_ALIASES.get(account_id, ()))}
        for alias in aliases:
            start = folded.find(alias)
            if start >= 0:
                matches.append((start, -len(alias), account_id))
    ordered: list[str] = []
    for _start, _negative_length, account_id in sorted(matches):
        if account_id not in ordered:
            ordered.append(account_id)
    return ordered


def product_order(products: list[dict], facts: dict, text: str,
                  subintents: list[str], *, exclude_ids: set[str] | None = None,
                  limit: int = 2) -> list[dict]:
    named = normalize_product(facts.get("account_product"), products)
    if named is None:
        named = normalize_product(text, products)

    def score(row: dict) -> tuple[int, str]:
        account_id = row["account_id"]
        value = 0
        if account_id == named:
            value += 100
        # Account-type weight applies on every path (eligibility, other, …):
        # without it non-selection routes degenerate to alphabetical order,
        # which put the supplementary Global Money ahead of Premier Checking.
        value += 30 if row.get("account_type") in {"current", "checking", "integrated"} else -10
        # Supplementary accounts that require an existing HSBC relationship
        # (e.g. US Global Money needs a Premier account as funding source) must
        # not lead a new-customer shortlist.
        if account_id == "hsbc_us_global_money":
            value += 5 if facts.get("existing_hsbc_customer") else -20
        # Minor-specific products: boost only for a known under-18 applicant,
        # deprioritize otherwise (unknown age or adult).
        if account_id in {"hsbc_hk_children_savings", "hsbc_hk_teens"}:
            age = facts.get("applicant_age")
            if isinstance(age, int):
                value += 50 if age < 18 else -50
            else:
                value -= 10
        if account_id == "hsbc_hk_one":
            value += 20
        if account_id == "hsbc_uk_bank":
            value += 20
        if account_id == "hsbc_uk_student":
            if facts.get("uk_residency_years") is not None:
                value += 30 if facts["uk_residency_years"] >= 3 else -20
            if "学生账户" in text or "student" in text.casefold():
                value += 20
        if "product_selection" in subintents:
            folded = text.casefold()
            wants_savings = bool(re.search(r"储蓄|存钱|savings?", folded, re.I))
            wants_fixed = bool(re.search(r"定期|存单|\bcd\b|fixed", folded, re.I))
            if wants_fixed:
                value += 60 if any(token in account_id for token in ("fixed_rate", "_cd", "time_deposit")) else 0
            elif wants_savings:
                value += 45 if row.get("account_type") == "savings" else 0
            elif row.get("customer_segment") == "general":
                value += 10
            if facts.get("role") == "student" or facts.get("applicant_identity") == "student":
                if row.get("customer_segment") in {"student", "international_student"}:
                    value += 35
            if account_id == "hsbc_us_premier_checking":
                value += 35
            if any(token in account_id for token in ("fixed_rate", "_cd", "time_deposit")) and not wants_fixed:
                value -= 25
        return -value, account_id

    excluded = set(exclude_ids or ())
    ordered = [row for row in sorted(products, key=score)
               if row["account_id"] not in excluded]
    alternative_request = bool(re.search(r"别的|其他|另外|换一个|之外", text))
    if "product_selection" in subintents and alternative_request and len(ordered) > 1:
        if named:
            ordered = [row for row in ordered if row["account_id"] != named]
        else:
            ordered = [*ordered[1:], ordered[0]]
    if named and "product_selection" in subintents:
        selected = [row for row in ordered if row["account_id"] == named]
        if selected:
            return selected
    if named and "product_selection" not in subintents:
        selected = [row for row in ordered if row["account_id"] == named]
        if named == "hsbc_uk_student" and facts.get("uk_residency_years") is not None \
                and facts["uk_residency_years"] < 3:
            selected += [row for row in ordered if row["account_id"] == "hsbc_uk_bank"]
        if named == "hsbc_hk_one" and isinstance(facts.get("applicant_age"), int) \
                and facts["applicant_age"] < 18:
            selected += [row for row in ordered
                         if row["account_id"] == "hsbc_hk_children_savings"]
        return selected
    return ordered[:limit] if "product_selection" in subintents else ordered[:1]


def evaluate_rule(rule: dict, facts: dict) -> str:
    operator = rule["operator"]
    if operator == "note":
        return "bank_confirmation"
    value = facts.get(rule["field_code"])
    if value is None and rule["field_code"] == "applicant_age" and \
            facts.get("applicant_age_band") in {"under_18", "18_or_over"} and \
            operator in {"gte", "lte"}:
        target = Decimal(str(rule["value_numeric"]))
        band = facts["applicant_age_band"]
        # The two-choice band is decisive only at the 18-year boundary. Other
        # age rules still require an exact age instead of inventing one.
        if target == 18:
            if operator == "gte":
                return "true" if band == "18_or_over" else "false"
            return "false" if band == "18_or_over" else "true"
    if value is None:
        return "unknown"
    try:
        if operator == "present":
            return "true" if value not in ("", []) else "false"
        if operator in ("gte", "lte"):
            actual = Decimal(str(value))
            target = Decimal(str(rule["value_numeric"]))
            passed = actual >= target if operator == "gte" else actual <= target
        elif operator == "eq":
            target = rule["value_text"]
            if isinstance(value, bool):
                value = str(value).lower()
            passed = str(value).casefold() == str(target).casefold()
        elif operator == "in":
            passed = str(value).casefold() in {part.strip().casefold()
                                               for part in str(rule["value_text"]).split("|")}
        else:
            return "bank_confirmation"
    except (InvalidOperation, TypeError, ValueError):
        return "unknown"
    return "true" if passed else "false"


def combine_status(evaluations: list[dict]) -> str:
    states = {item["result"] for item in evaluations}
    if "false" in states:
        return "does_not_meet_published_criteria"
    if "unknown" in states:
        return "needs_input"
    if "bank_confirmation" in states:
        return "requires_bank_confirmation"
    return "meets_published_criteria"


def evaluate_decision_path(path: dict, facts: dict) -> dict:
    """AND within each alternative group, OR between groups; unknown never passes."""
    groups: dict[str, list[str]] = {}
    evaluated = []
    for condition in path.get("conditions", []):
        if condition["operator"] == "bank_confirm":
            result = "bank_confirmation"
        else:
            if condition["field_code"] == "applicant_identity" and facts.get("account_applicant_relation") == "child":
                result = "true"
            else:
                result = evaluate_rule(condition, facts)
        evaluated.append({"condition": condition, "result": result})
        if condition.get("required", True):
            groups.setdefault(condition["group_code"], []).append(result)
    outcomes = [combine_status([{"result": result} for result in values])
                for values in groups.values()]
    if not outcomes:
        status = "requires_bank_confirmation"
    elif "meets_published_criteria" in outcomes:
        status = "meets_published_criteria"
    elif "needs_input" in outcomes:
        status = "needs_input"
    elif "requires_bank_confirmation" in outcomes:
        status = "requires_bank_confirmation"
    else:
        status = "does_not_meet_published_criteria"
    if path.get("path_status") != "published" and status == "meets_published_criteria":
        status = "requires_bank_confirmation"
    return {"path_id": path["path_id"], "status": status,
            "conditions": evaluated, "evidence": path.get("evidence", []),
            "description": path["description"],
            "predeparture_stage": path["predeparture_stage"]}


def assess_product(product: dict, catalog: dict, facts: dict) -> dict:
    account_id = product["account_id"]
    application = [row for row in catalog["rules"] if row["account_id"] == account_id
                   and row["rule_purpose"] == "application"]
    evaluated = [{"rule": row, "result": evaluate_rule(row, facts)} for row in application]
    routes = []
    document_type = facts.get("identity_document_type")
    for route in catalog["routes"]:
        if route["account_id"] != account_id:
            continue
        route_rules = [row for row in catalog["rules"] if row["route_id"] == route["route_id"]
                       and row["rule_purpose"] == "route"]
        route_evaluated = [{"rule": row, "result": evaluate_rule(row, facts)}
                           for row in route_rules]
        if document_type and account_id == "hsbc_hk_one":
            expected = {"hk_one_hkid_app": "hkid", "hk_one_passport_branch": "passport",
                        "hk_one_eep_app": "eep"}.get(route["route_id"])
            if expected and document_type != expected:
                route_evaluated.append({"rule": None, "result": "false",
                                        "description": "证件与该申请渠道不匹配"})
        routes.append({"route": route, "status": combine_status(route_evaluated),
                       "rule_results": route_evaluated})
    return {"product": product, "status": combine_status(evaluated),
            "rule_results": evaluated, "routes": routes,
            "documents": [row for row in catalog["documents"] if row["account_id"] == account_id],
            "fees": [row for row in catalog["fees"] if row["account_id"] == account_id],
            "features": [row for row in catalog.get("features", [])
                         if row["account_id"] == account_id],
            "fee_waivers": [row for row in catalog["rules"] if row["account_id"] == account_id
                            and row["rule_purpose"] == "fee_waiver"]}
