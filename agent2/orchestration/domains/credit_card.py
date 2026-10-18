"""Credit-card recommendation workflow backed by the shared RDS product tables."""

import re
from datetime import datetime, timedelta, timezone

from agent2.models import AnswerBody, FollowUpQuestion, Notice

from ..contracts import DomainResult, DomainTask, OrchestrationInput
from .credit_card_engine import recommend
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


def extract_credit_card_facts(text: str, asked_fields=()):
    lower = text.casefold()
    facts, basis = {}, {}
    for region, words in REGION_WORDS.items():
        word = next((word for word in words if word.casefold() in lower), None)
        position = lower.find(word.casefold()) if word else -1
        card_positions = [match.start() for match in re.finditer(r"信用卡|credit\s*card", lower, re.I)]
        region_was_asked = "application_region" in asked_fields
        near_card_intent = any(abs(position - card_position) <= 8 for card_position in card_positions)
        if word and (region_was_asked or near_card_intent):
            facts["application_region"], basis["application_region"] = region, word
            break
    income = re.search(r"(?:年收入|年薪)\D{0,8}(\d+(?:\.\d+)?)\s*(万|[kK])?", text)
    if income:
        value = float(income.group(1))
        value *= 10000 if income.group(2) == "万" else 1000 if income.group(2) else 1
        facts["annual_income_hkd"], basis["annual_income_hkd"] = int(value), income.group(0)
    elif "annual_income_hkd" in asked_fields:
        amount = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*(万|[kK])?(?:港币|HKD)?\s*", text, re.I)
        if amount:
            value = float(amount.group(1))
            value *= 10000 if amount.group(2) == "万" else 1000 if amount.group(2) else 1
            facts["annual_income_hkd"], basis["annual_income_hkd"] = int(value), text.strip()
    if "applicant_age" in asked_fields and "applicant_age" not in facts:
        age = re.search(r"(?:申请人年龄\D{0,4})?(\d{1,3})\s*(?:岁)?", text)
        if age:
            facts["applicant_age"], basis["applicant_age"] = int(age.group(1)), text.strip()
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
    "application_region": [{"value": "CN-HK", "label": "中国香港"}],
    "applicant_age_band": [{"value": "under_18", "label": "18 岁以下"},
                           {"value": "18_or_over", "label": "18 岁或以上"}],
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
}


class CreditCardWorkflow:
    def __init__(self, repository: CreditCardRepository | None = None, clock=None):
        self.repository = repository or RdsCreditCardRepository()
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    async def run(self, task: DomainTask, request: OrchestrationInput) -> DomainResult:
        extracted, basis = extract_credit_card_facts(request.text, request.asked_fields)
        facts = {**request.profile_values, **task.facts, **extracted}

        if facts.get("application_region") is None:
            missing = ["application_region"]
            if facts.get("applicant_age") is None and facts.get("applicant_age_band") is None:
                missing.append("applicant_age_band")
            prompts = {
                "application_region": "你准备在哪个地区申请信用卡？目前 RDS 只有香港产品数据。",
                "applicant_age_band": "你目前是否已满 18 岁？",
            }
            return self._needs_input(task, request, extracted, basis, missing, prompts, QUESTION_OPTIONS)

        catalog = None
        if facts.get("annual_income_hkd") is None and facts.get("annual_income_band_hkd") is None:
            try:
                catalog = await self.repository.active_products(facts["application_region"], self.clock())
            except CreditCardRepositoryError:
                return self._repository_unavailable(task, extracted, basis)
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

        missing = [field for field in ("repayment_mode", "spending_scenarios") if not facts.get(field)]
        if missing:
            prompts = {
                "repayment_mode": "你通常会每月全额还款，还是可能分期或保留余额？",
                "spending_scenarios": "最主要的两到三个消费场景是什么？例如网购、餐饮、旅行、内地消费或学费。",
            }
            return self._needs_input(task, request, extracted, basis, missing, prompts, QUESTION_OPTIONS)

        if facts["repayment_mode"] != "full":
            return DomainResult(
                task_id=task.task_id, domain="credit_card", status="needs_verification",
                answer=AnswerBody(
                    summary="你可能分期或保留余额，应优先比较借款成本；当前 RDS 没有 APR、分期利率和免息期数据。",
                    limitations=["在借款成本资料补齐前，不按返现或积分推荐信用卡。"],
                ),
                missing_fields=["card_apr_data"],
                next_actions=["补齐信用卡 APR、分期手续费和免息期数据后重新比较。"],
                notices=[Notice(code="borrowing_cost_data_missing", message="RDS 缺少信用卡借款成本字段")],
                observed_facts=extracted, observed_basis=basis,
            )

        if catalog is None:
            try:
                catalog = await self.repository.active_products(facts["application_region"], self.clock())
            except CreditCardRepositoryError:
                return self._repository_unavailable(task, extracted, basis)
            if not catalog.products:
                return self._empty_catalog(task, catalog, extracted, basis)

        scenarios = facts["spending_scenarios"] if isinstance(facts["spending_scenarios"], list) else \
            [item for item in str(facts["spending_scenarios"]).split(",") if item]
        result = recommend(catalog.products, facts, scenarios, facts.get("reward_preference", "no_preference"))
        if result.primary is None:
            return DomainResult(
                task_id=task.task_id, domain="credit_card", status="cannot_answer",
                answer=AnswerBody(summary="现有产品均明确不满足你的申请门槛。"),
                payload={"recommendations": result.model_dump(mode="json")},
                knowledge_read_id=catalog.read_id, observed_facts=extracted, observed_basis=basis,
            )

        recommendations = [result.primary, *result.supplements, *result.alternatives]
        now = self.clock()
        by_id = {product.card_id: product for product in catalog.products}
        data_ready = all(
            card.data_status == "verified" and card.official_url
            and by_id[card.card_id].last_verified_at
            and now - by_id[card.card_id].last_verified_at <= timedelta(hours=24)
            for card in recommendations
        )
        steps = [self._format_card(result.primary)]
        steps += [self._format_card(card) for card in result.supplements]
        if result.alternatives:
            steps.append("免年费备选：" + "、".join(card.product_name for card in result.alternatives))
        limitations = list(dict.fromkeys(limit for card in recommendations for limit in card.limitations))
        if not data_ready:
            limitations.insert(0, "【模拟推荐】RDS 当前信用卡记录均为 synthetic，且缺少官方链接和核验时间。")
        evidence = [card.official_url for card in recommendations
                    if card.data_status == "verified" and card.official_url]
        return DomainResult(
            task_id=task.task_id, domain="credit_card",
            status="answered" if data_ready else "needs_verification",
            answer=AnswerBody(summary=f"主推荐：{result.primary.product_name}", steps=steps,
                              limitations=limitations),
            evidence_refs=evidence,
            next_actions=["申请前在发卡机构官网确认资格、年费、奖励上限和最新条款。"],
            notices=[] if data_ready else [Notice(code="synthetic_card_catalog",
                                                   message="推荐使用了 RDS synthetic 信用卡数据")],
            payload={"recommendations": result.model_dump(mode="json"), "market": catalog.market},
            knowledge_read_id=catalog.read_id, observed_facts=extracted, observed_basis=basis,
        )

    @staticmethod
    def _needs_input(task, request, extracted, basis, missing, prompts, options):
        selected = missing[:2]
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

    @staticmethod
    def _format_card(card):
        label = {"primary": "主推荐", "supplement": "补充卡", "alternative": "备选"}[card.role]
        scenes = "、".join(card.matched_scenarios) or "通用消费"
        fee = "年费资料缺失" if card.annual_fee is None else \
            f"年费 {card.annual_fee:g} {card.annual_fee_currency or ''}".strip()
        if card.annual_fee_waiver:
            fee += f"；减免条件：{card.annual_fee_waiver}"
        return f"{label}：{card.product_name}；匹配场景：{scenes}；{fee}；资格：{card.eligibility.status}。"
