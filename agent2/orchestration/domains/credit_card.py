"""Credit-card recommendation workflow backed by the shared RDS product tables."""

import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from agent2.models import AnswerBody, FollowUpQuestion, Notice

from ..contracts import DomainResult, DomainTask, OrchestrationInput
from .credit_card_engine import SCENARIO_RULES, assess_eligibility, needs_asset_information, recommend
from .credit_card_models import CardCatalogRead, CreditCardRecommendationSet
from .credit_card_repository import (CreditCardRepository, CreditCardRepositoryError,
                                     RdsCreditCardRepository)

SCENARIO_WORDS = {
    "online": ("网购", "线上", "电商", "online"),
    "dining": ("餐饮", "吃饭", "餐厅", "dining"),
    "travel": ("旅行", "旅游", "机票", "酒店", "travel"),
    "overseas": ("境外", "海外", "外币", "overseas"),
    "mainland_china": ("内地", "大陆", "mainland"),
    "tuition": ("学费", "tuition"),
    "retail": ("超市", "购物", "零售", "retail"),
}
REGION_WORDS = {"CN-HK": ("香港", "hong kong", "hk"), "UK": ("英国", "uk"),
                "US": ("美国", "us"), "CN": ("内地", "大陆", "中国")}


def extract_credit_card_facts(text: str):
    lower = text.casefold()
    facts, basis = {}, {}
    for region, words in REGION_WORDS.items():
        word = next((word for word in words if word.casefold() in lower), None)
        position = lower.find(word.casefold()) if word else -1
        card_positions = [match.start() for match in re.finditer(r"信用卡|credit\s*card", lower, re.I)]
        near_card_intent = any(abs(position - card_position) <= 8 for card_position in card_positions)
        if word and near_card_intent:
            facts["application_region"], basis["application_region"] = region, word
            break
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
        basis["spending_scenarios"] = "、".join(scenarios)
    if re.search(r"全日制.{0,4}学生|学生卡", text):
        facts["full_time_student"], basis["full_time_student"] = True, "全日制学生"
    if re.search(r"卓越理财|premier", text, re.I):
        facts["premier_relationship"], basis["premier_relationship"] = True, "HSBC Premier"
    for value, words in {"cashback": ("返现", "现金回赠"), "points": ("积分",),
                         "miles": ("里程", "航空里数")}.items():
        if any(word in text for word in words):
            facts["reward_preference"], basis["reward_preference"] = value, next(w for w in words if w in text)
            break
    return facts, basis


def _question(field, text, version, *, options, input_type="single_select", max_selections=None):
    return FollowUpQuestion(question_code=f"ask_{field}", field=field, text=text,
                            rule="credit_card_recommendation_v1", profile_version=version,
                            input_type=input_type, options=options, max_selections=max_selections,
                            persist_mode="save_on_select")


def _income_options(products):
    thresholds = sorted({int(rule.value_numeric) for product in products
                         for rule in product.eligibility_rules
                         if rule.required and rule.rule_type == "minimum_income"
                         and rule.currency == "HKD" and rule.value_numeric is not None})
    if not thresholds:
        return []
    options = [{"value": f"lt_{thresholds[0]}", "label": f"少于 HK${thresholds[0]:,}"}]
    for lower, upper in zip(thresholds, thresholds[1:]):
        options.append({"value": f"range_{lower}_{upper - 1}",
                        "label": f"HK${lower:,}–{upper - 1:,}"})
    options.append({"value": f"gte_{thresholds[-1]}", "label": f"HK${thresholds[-1]:,} 或以上"})
    return options


QUESTION_OPTIONS = {
    "application_region": [{"value": "UK", "label": "英国"},
                           {"value": "US", "label": "美国"},
                           {"value": "CN-HK", "label": "中国香港"}],
    "applicant_age_band": [{"value": "under_18", "label": "18 岁以下"},
                           {"value": "18_or_over", "label": "18 岁或以上"}],
    "hk_resident": [{"value": True, "label": "是，我是香港居民"},
                    {"value": False, "label": "否，我不是香港居民"}],
    "us_hsbc_account_path": [
        {"value": "existing_premier_checking",
         "label": "已有美国汇丰 Premier 支票账户"},
        {"value": "plan_to_open",
         "label": "到美国后计划先开 Premier 支票账户，再申请信用卡"},
        {"value": "not_applicable",
         "label": "暂时没有，也不考虑开美国汇丰账户"},
    ],
    "uk_hsbc_account_path": [
        {"value": "premier_account",
         "label": "是，已有英国 HSBC Premier / Private Banking"},
        {"value": "plan_to_open",
         "label": "否，优先看学生卡（需先开英国学生账户）"},
    ],
    "hsbc_relationship_balance_band": [
        {"value": "under_100k", "label": "少于 HK$100,000"},
        {"value": "100k_999k", "label": "HK$100,000–999,999"},
        {"value": "1m_plus", "label": "HK$1,000,000 或以上"},
    ],
    "hsbc_premier_path": [
        {"value": "existing_premier",
         "label": "已经是 HSBC Premier 客户，并可满足信用卡关系结余要求"},
        {"value": "can_qualify_with_1m_trb",
         "label": "可在 HSBC 维持 HK$1,000,000 总关系结余，并愿意先升级 Premier"},
        {"value": "not_applicable", "label": "以上均不适用，或不考虑 Premier"},
    ],
    "asset_band": [
        {"value": "none", "label": "目前无法提供银行资产证明"},
        {"value": "under_100k", "label": "可证明资产少于 HK$100,000"},
        {"value": "100k_500k", "label": "可证明资产 HK$100,000–500,000"},
        {"value": "500k_plus", "label": "可证明资产 HK$500,000 或以上"},
    ],
    "full_time_student": [
        {"value": True, "label": "是，我是全日制学生"},
        {"value": False, "label": "否，我不是全日制学生"},
    ],
    "student_institution_eligible": [
        {"value": True, "label": "是，我就读 HSBC 指定名单内的香港院校课程"},
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
    "study_spending_scenarios": [
        {"value": "groceries", "label": "超市 / 日常采购"},
        {"value": "travel", "label": "旅行 / 机票 / 贵宾室权益"},
        {"value": "dining", "label": "餐饮"},
        {"value": "online", "label": "网购"},
        {"value": "retail", "label": "日常零售"},
    ],
    "monthly_card_spend_band_hkd": [
        {"value": "under_2k", "label": "少于 HK$2,000"},
        {"value": "2k_5k", "label": "HK$2,000–4,999"},
        {"value": "5k_10k", "label": "HK$5,000–9,999"},
        {"value": "10k_20k", "label": "HK$10,000–19,999"},
        {"value": "20k_plus", "label": "HK$20,000 或以上"},
    ],
    "mainland_payment_method": [
        {"value": "mobile_or_qr", "label": "主要使用手机支付或银联二维码"},
        {"value": "physical_card", "label": "主要刷实体卡"},
        {"value": "mixed_or_unsure", "label": "两者差不多，或暂不确定"},
    ],
    "reward_preference": [
        {"value": "cashback", "label": "现金回赠"},
        {"value": "points", "label": "积分 / MoneyBack Points"},
        {"value": "miles", "label": "航空里程"},
        {"value": "no_preference", "label": "没有偏好，按预计价值比较"},
    ],
}


# Study destination → HSBC cards issued in that market only. Products from
# different jurisdictions are never mixed into one recommendation.
STUDY_MARKETS = {"UK": ("UK",), "US": ("US",)}
# Profile destination_country can stand in for application_region when it is a
# known card market (including Hong Kong — do not re-ask where they study).
DESTINATION_AS_REGION = frozenset({"UK", "US", "CN-HK"})

GROUP_TITLES = {
    "UK": "汇丰英国发行的卡",
    "US": "汇丰美国发行的卡",
    "CN-HK": "汇丰香港发行的卡",
}
# Official eligibility, stated as a reminder. Unmet prerequisites do not drop the card.
PREREQUISITES = {
    "US": (
        "到美国读书：申请汇丰美国卡通常须已有美国汇丰 Premier 支票账户"
        "（内地或香港的 Premier 不算）。还没有该账户时，下面仍列出产品供了解，"
        "不代表现在可以提交或获批。"
    ),
    "UK": (
        "到英国读书：学生卡一般须年满 18 岁、为英国居民，并已持有汇丰英国学生账户；"
        "Premier 卡须已有汇丰英国 Premier 或 Private Banking 关系。"
        "从内地出发、账户尚未开立时，通常需到英国后再申请。下面仍列出产品供了解。"
    ),
    "CN-HK": (
        "香港卡可在留学地刷 Visa / Mastercard；外币手续费以该卡官网为准。"
    ),
}

_STATUS_ORDER = {"eligible": 0, "needs_bank_assessment": 1, "unknown": 2, "ineligible": 3}


class CreditCardWorkflow:
    def __init__(self, repository: CreditCardRepository | None = None, clock=None):
        self.repository = repository or RdsCreditCardRepository()
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    async def run(self, task: DomainTask, request: OrchestrationInput) -> DomainResult:
        if request.input_mode == "free_text":
            rule_facts, rule_basis = extract_credit_card_facts(request.text)
        else:
            rule_facts, rule_basis = {}, {}
        extracted = {**rule_facts, **request.understood_facts}
        basis = {**rule_basis, **request.understood_basis}
        facts = {**request.profile_values, **task.facts, **extracted}
        destination = facts.get("destination_country")
        if facts.get("application_region") is None and destination in DESTINATION_AS_REGION:
            facts["application_region"] = destination
            extracted["application_region"] = destination
            basis["application_region"] = "留学目的地"

        if facts.get("application_region") is None:
            missing = ["application_region"]
            if facts.get("applicant_age") is None and facts.get("applicant_age_band") is None:
                missing.append("applicant_age_band")
            prompts = {
                "application_region": (
                    "你准备去哪里留学？系统只会推荐你所选地区发行的信用卡。"
                ),
                "applicant_age_band": "你目前是否已满 18 岁？",
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
                prompts["applicant_age_band"] = "你目前是否已满 18 岁？"
            missing.append("annual_income_band_hkd")
            prompts["annual_income_band_hkd"] = "请选择你的税前年收入区间；选项按当前产品门槛生成。"
            options["annual_income_band_hkd"] = _income_options(catalog.products)
            return self._needs_input(
                task, request, extracted, basis, missing, prompts, options)

        if facts.get("applicant_age") is None and facts.get("applicant_age_band") is None:
            return self._needs_input(task, request, extracted, basis, ["applicant_age_band"],
                                     {"applicant_age_band": "你目前是否已满 18 岁？"}, QUESTION_OPTIONS)

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
                    "你目前是否为香港特别行政区居民？HSBC 将其列为基本卡申请条件；"
                    "非香港居民仍可尝试申请，但获批几率很低，将按个案审批展示。"
                )},
                QUESTION_OPTIONS,
            )

        early_assessments = [assess_eligibility(product, facts) for product in catalog.products]
        if early_assessments and all(item.status == "ineligible" for item in early_assessments):
            excluded = {
                product.card_id: assessment.reasons
                for product, assessment in zip(catalog.products, early_assessments)
            }
            steps = [
                f"{product.product_name}：{'；'.join(excluded[product.card_id])}"
                for product in catalog.products
            ]
            return DomainResult(
                task_id=task.task_id, domain="credit_card", status="cannot_answer",
                answer=AnswerBody(summary="现有产品均明确不满足你的申请门槛。", steps=steps),
                payload={"recommendations": CreditCardRecommendationSet(
                    excluded=excluded).model_dump(mode="json"),
                         "catalog": self._catalog_summary(catalog)},
                knowledge_read_id=catalog.read_id, observed_facts=extracted, observed_basis=basis,
            )

        has_premier_path = any(
            rule.required and rule.rule_type == "banking_relationship"
            for product in gate_products for rule in product.eligibility_rules
        )
        if (has_premier_path and facts.get("hsbc_premier_path") is None
                and facts.get("premier_relationship") is None):
            return self._needs_input(
                task, request, extracted, basis, ["hsbc_premier_path"],
                {"hsbc_premier_path": (
                    "以下哪种 HSBC Premier 情况与你相符？这里的 HK$1,000,000 指在 HSBC 的"
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
                    "你已确认是 HSBC Premier 客户。为核对 Premier Mastercard 的申请条件，"
                    "请选择你在 HSBC 的总关系结余区间。"
                )},
                QUESTION_OPTIONS,
            )

        needs_asset = any(needs_asset_information(product, facts) for product in gate_products)
        if needs_asset and facts.get("asset_band") is None:
            return self._needs_input(
                task, request, extracted, basis, ["asset_band"],
                {"asset_band": (
                    "你的收入未达到部分卡的公开门槛。你能否提供最近3个月的银行资产证明，"
                    "以及可证明资产大致在哪个区间？这条路径仍需HSBC个案审批。"
                )},
                QUESTION_OPTIONS,
            )

        student_questions = self._student_questions_needed(gate_products, facts)
        if student_questions:
            prompts = {
                "full_time_student": (
                    "你的收入未达到一般信用卡的公开门槛。你是否为全日制学生？"
                    "学生信用卡不设收入要求。"
                ),
                "student_institution_eligible": (
                    "你是否就读于 HSBC 指定名单内的香港全日制院校课程？"
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
                "repayment_mode": "你通常会每月全额还款，还是可能分期或保留余额？",
                "spending_scenarios": (
                    "请按重要程度选择最主要的两到三个消费场景；先选的场景权重更高。"
                    "例如网购、餐饮、旅行、内地消费或学费。"
                ),
                "monthly_card_spend_band_hkd": (
                    "你预计每月信用卡消费大约在哪个区间？这用于计算奖励上限、年费和外币手续费。"
                ),
                "reward_preference": "你更偏好现金回赠、积分、航空里程，还是按预计价值比较？",
                "mainland_payment_method": (
                    "你在中国内地消费时通常怎样付款？Pulse 的 4.4% 是组合上限，"
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
                    summary="你可能分期或保留余额，应优先比较借款成本；RDS 的借款成本资料还不完整。",
                    limitations=["借款成本资料补齐前，不按返现或积分给出确定推荐。"],
                ),
                missing_fields=["card_apr_data"],
                next_actions=["补齐信用卡 APR、分期手续费和免息期数据后重新比较。"],
                notices=[Notice(code="borrowing_cost_data_missing", message="RDS 缺少信用卡借款成本字段")],
                observed_facts=extracted, observed_basis=basis,
            )

        scenarios = facts["spending_scenarios"] if isinstance(facts["spending_scenarios"], list) else \
            [item for item in str(facts["spending_scenarios"]).split(",") if item]
        result = recommend(catalog.products, facts, scenarios,
                           facts.get("reward_preference", "no_preference"), now=self.clock())
        if result.primary is None:
            names = {product.card_id: product.product_name for product in catalog.products}
            steps = [
                f"{names.get(card_id, card_id)}：{'；'.join(reasons)}"
                for card_id, reasons in result.excluded.items()
            ]
            return DomainResult(
                task_id=task.task_id, domain="credit_card", status="cannot_answer",
                answer=AnswerBody(summary="现有产品均明确不满足你的申请门槛。", steps=steps),
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
            steps.append("免年费备选：" + "、".join(card.product_name for card in result.alternatives))
        limitations = list(dict.fromkeys(
            limit for card in recommendations for limit in card.limitations
            if limit and not limit.startswith(("满足", "不满足", "缺少门槛", "非香港居民"))))
        # Non-resident applicants: HSBC lists HK residency as a written basic
        # requirement, yet a small number of cases do get approved. The cards
        # stay recommended with a prominent low-approval-chance disclosure.
        if any(card.eligibility.low_approval_chance for card in recommendations):
            limitations.insert(0, (
                "你不是香港居民：汇丰香港成文条件要求申请人一般为香港居民，"
                "获批几率很低，以下推荐仅为参考，最终由银行审批。"))
        if not product_data_ready:
            limitations.insert(0, "部分年费或奖赏资料仍待官网核验，请以发卡机构页面为准。")
        # Time-limited promotional layers must be disclosed, not buried in a rate.
        promo_notes = list(dict.fromkeys(
            note for card in recommendations for note in card.promo_rules))
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
                "「最红自主奖赏」自选类别的每年首 HK$100,000 加速奖赏以身份证合计、"
                "跨卡共享；多卡组合时不能各自再享一次上限。")
        limitations.append("目前仅覆盖 HSBC 香港信用卡，暂不比较其他发卡机构的产品。")
        limitations.append(
            "迎新礼遇（如首年额外现金回赠或飞行里程）属一次性优惠，未计入年度净值估算；"
            "申请前可在官网查看最新迎新条款。")
        evidence = list(dict.fromkeys(
            [card.official_url for card in recommendations if card.official_url]
            + [source for card in recommendations for source in card.borrowing_sources]))
        return DomainResult(
            task_id=task.task_id, domain="credit_card",
            status="answered" if product_data_ready else "needs_verification",
            answer=AnswerBody(summary=f"主推荐：{result.primary.product_name}", steps=steps,
                              limitations=limitations),
            evidence_refs=evidence,
            next_actions=["申请前在发卡机构官网确认资格、年费、奖励上限和最新条款。"],
            notices=[] if product_data_ready else [Notice(
                code="card_product_fields_need_verification",
                message="信用卡奖励和年费字段尚未逐项完成官方核验")],
            payload={"recommendations": result.model_dump(mode="json"), "market": catalog.market,
                     "catalog": self._catalog_summary(catalog)},
            knowledge_read_id=catalog.read_id, observed_facts=extracted, observed_basis=basis,
        )

    async def _run_study(self, task, request, facts, extracted, basis):
        """UK/US study path: recommend cards issued in the selected market only.

        Do not reuse Hong Kong income / Premier TRB / designated-school questions.
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

        account_field = "us_hsbc_account_path" if region == "US" else "uk_hsbc_account_path"
        if region == "UK" and facts.get(account_field) is None:
            # Map a generic Premier flag onto the local path when present; otherwise ask.
            if facts.get("premier_relationship") is True:
                inferred = ("existing_premier_checking" if region == "US" else "premier_account")
                facts = {**facts, account_field: inferred}
                extracted[account_field] = inferred
                basis[account_field] = "premier_relationship"
            else:
                prompts = {
                    "us_hsbc_account_path": (
                        "申请汇丰美国信用卡通常须已有美国汇丰 Premier 支票账户"
                        "（内地或香港的 Premier 不算）。你目前属于哪种情况？"
                    ),
                    "uk_hsbc_account_path": (
                        "你是否已有英国 HSBC Premier 或 Private Banking 账户？"
                    ),
                }
                return self._needs_input(
                    task, request, extracted, basis, [account_field], prompts, QUESTION_OPTIONS)

        # Without UK Premier, lead with the Student Card and disclose that a
        # UK Student Bank Account must be opened first. No second question can
        # improve that recommendation.
        if region == "UK" and facts.get(account_field) in {
                "student_account", "plan_to_open", "not_applicable"}:
            facts = {**facts, "spending_scenarios": ["retail"]}
        elif not facts.get("spending_scenarios"):
            options = dict(QUESTION_OPTIONS)
            options["spending_scenarios"] = QUESTION_OPTIONS["study_spending_scenarios"]
            result = self._needs_input(
                task, request, extracted, basis, ["spending_scenarios"],
                {"spending_scenarios": (
                    "你最看重哪类用途？请选 1–2 项，这会直接决定卡片排序。"
                )},
                options,
            )
            result.follow_up_questions[0].max_selections = 2
            return result

        scenarios = facts["spending_scenarios"] if isinstance(facts["spending_scenarios"], list) else \
            [item for item in str(facts["spending_scenarios"]).split(",") if item]
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

    def _study_answer(self, task, catalogs, facts, scenarios, extracted, basis):
        """Recommend and rank the HSBC shelf from the selected market only."""
        steps, evidence, limitations = [], [], []
        groups = []
        local_products = []
        local_shown = []
        local_title = None
        region = facts["application_region"]
        for market, catalog in catalogs:
            local_title = GROUP_TITLES[market]
            steps.append(local_title)
            steps.append(PREREQUISITES[market])
            local_products = catalog.products
            lines, local_shown = self._local_lines(
                catalog.products, facts, scenarios, facts.get("reward_preference", "points"), market)
            if not catalog.products:
                steps.append("当地汇丰信用卡产品暂未就绪；申请前请到当地汇丰官网查看。")
            else:
                steps.extend(lines)
            groups.append({"market": catalog.market, "role": "primary", "cards": local_shown})
            evidence.extend(product.official_url for product in catalog.products
                            if product.official_url)

        eligible_local = next(
            (item["product_name"] for item in local_shown
             if item.get("eligibility") == "eligible"),
            None,
        )
        if eligible_local:
            summary = (
                f"留学{ {'UK': '英国', 'US': '美国'}.get(region, region) }优先考虑当地汇丰卡，"
                f"主推 {eligible_local}。"
            )
        elif local_products:
            summary = (
                f"留学{ {'UK': '英国', 'US': '美国'}.get(region, region) }优先列出汇丰当地卡；"
                "若尚未具备当地账户条件，下面仍供了解。"
            )
        else:
            summary = "所选地区的汇丰信用卡资料暂缺。"

        limitations.append(
            "结果仅包含所选申请地区发行的信用卡，不混入其他地区产品。")
        limitations.append("未满足当地申请条件的卡仅供了解，不代表现在可以提交或获批。")
        limitations.append("迎新礼遇未计入年度净值，申请前请以官网最新条款为准。")

        now = self.clock()
        ready_products = local_products
        product_data_ready = bool(ready_products) and all(
            product.official_url
            and product.last_verified_at
            and now - product.last_verified_at <= timedelta(days=30)
            and all(item.source_url and item.last_verified_at
                    for item in [*product.reward_rules, *product.fees, *product.benefits])
            for product in ready_products
        )
        if not product_data_ready:
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

    @staticmethod
    def _local_lines(products, facts, scenarios, preference, market):
        """Rank one local-market shelf without comparing values across currencies.

        Eligibility remains the primary ordering key. Within the same eligibility
        tier, prefer products whose reward rules cover the user's ordered spending
        scenarios, then honour an explicit points/miles/cashback preference.
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

        ranked = sorted(
            ((assess_eligibility(product, facts), product) for product in products),
            key=lambda item: (
                _STATUS_ORDER.get(item[0].status, 9),
                -fit(item[1]),
                item[1].product_name,
            ),
        )
        lines, shown = [], []
        for assessment, product in ranked:
            lines.extend(CreditCardWorkflow._plain_card_lines(product, assessment))
            shown.append({
                "card_id": product.card_id,
                "product_name": product.product_name,
                "eligibility": assessment.status,
                "reasons": assessment.reasons,
            })
        return lines, shown

    @staticmethod
    def _fee_text_from_product(product):
        annual = next((fee for fee in product.fees if fee.fee_type == "annual_fee"), None)
        if annual is None or annual.amount is None:
            text = "请见官网"
        elif annual.amount == 0:
            text = "免年费"
        else:
            text = f"{annual.amount:g} {annual.currency or ''}".strip()
            if annual.waiver_condition:
                text += f"（{annual.waiver_condition}）"
        fx = next((fee for fee in product.fees if fee.fee_type == "foreign_transaction_fee"), None)
        if fx is not None and fx.rate is not None:
            text += f"；外币手续费 {fx.rate * 100:g}%"
        return text

    @staticmethod
    def _perks_from_product(product):
        names = [benefit.benefit_name for benefit in product.benefits if benefit.benefit_name]
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
    def _plain_card_lines(product, assessment):
        network = product.card_network or "Visa/Mastercard"
        lines = [
            f"{product.product_name}（{network}）",
            f"年费：{CreditCardWorkflow._fee_text_from_product(product)}",
            f"主要权益：{CreditCardWorkflow._perks_from_product(product)}",
        ]
        restriction = CreditCardWorkflow._restriction_text(assessment)
        if restriction:
            lines.append(f"申请限制：{restriction}")
        return lines

    @staticmethod
    def _format_card_lines(card):
        """User-facing card blurb: fee, perks, then restrictions only if needed."""
        label = {"primary": "主推荐", "supplement": "补充卡", "alternative": "备选"}[card.role]
        if card.annual_fee is None:
            fee = "请见官网"
        elif card.annual_fee == 0:
            fee = "免年费"
            if card.annual_fee_waiver:
                fee += f"（{card.annual_fee_waiver}）"
        else:
            fee = f"{card.annual_fee:g} {card.annual_fee_currency or ''}".strip()
            if card.annual_fee_waiver:
                fee += f"（{card.annual_fee_waiver}）"
            if (card.ongoing_annual_fee_hkd is not None and card.ongoing_annual_fee_hkd > 0
                    and card.estimated_annual_fee_hkd == 0):
                fee += f"；豁免期后年费 HK${card.ongoing_annual_fee_hkd:,.0f}/年"
        perks = list(card.main_benefits[:5])
        scenes = [s for s in card.matched_scenarios if s]
        if scenes:
            perks.append("较适合：" + "、".join(scenes))
        miles = card.reward_units.get("miles")
        points = card.reward_units.get("points")
        if miles is not None and miles > 0:
            perks.append(f"约 {miles:,.0f} 飞行里程/年")
        elif points is not None and points > 0:
            perks.append(f"约 {points:,.0f} 积分/年")
        perk_text = "；".join(perks) if perks else "详见官网奖赏与附加权益"
        lines = [
            f"{label}：{card.product_name}",
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
            f"{term.tenor_months}期 APR {term.apr * 100:.2f}%、总还款 HK${term.total_repayment:,.0f}"
            for term in cash_examples)
        spending = by_type["spending_instalment"][0]
        cash_offer = by_type["cash_instalment_offer"][0]
        instalment = (
            f"现金分期当前公布每月手续费低至 {cash_offer.monthly_fee_rate * 100:.2f}%；"
            f"HK$100,000 官方示例：{examples}。消费分期手续费为个人报价，"
            "需在 HSBC HK App、Reward+ 或热线确认。"
            if spending.available else "HSBC 条款列明此卡不适用现金分期和消费分期。"
        )
        return (f"借款成本：消费 APR {purchase.apr * 100:.2f}%，最长免息期 "
                f"{free.max_interest_free_days} 天。{instalment}")

    @staticmethod
    def _needs_input(task, request, extracted, basis, missing, prompts, options):
        selected = missing[:1]
        return DomainResult(
            task_id=task.task_id, domain="credit_card", status="needs_input",
            answer=AnswerBody(summary="还需要一些信息才能筛选信用卡。",
                              steps=["先确认申请门槛，再了解还款方式和主要消费场景。"]),
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
            next_actions=["恢复 RDS 连接后重新运行信用卡推荐。"],
            notices=[Notice(code="credit_card_repository_unavailable", message="RDS 信用卡读取失败")],
            observed_facts=extracted, observed_basis=basis,
        )

    @staticmethod
    def _empty_catalog(task, catalog, extracted, basis):
        return DomainResult(
            task_id=task.task_id, domain="credit_card", status="cannot_answer",
            answer=AnswerBody(summary="RDS 中没有该地区当前有效的信用卡产品。"),
            next_actions=["接入该地区信用卡产品及门槛、奖励、费用和权益数据。"],
            knowledge_read_id=catalog.read_id, observed_facts=extracted, observed_basis=basis,
        )
