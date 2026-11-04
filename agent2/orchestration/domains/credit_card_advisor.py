"""Read-only customer-manager enrichment for credit-card recommendations.

The credit-card engine remains the source of ranking and eligibility.  This
module is deliberately downstream: it turns an already selected card into a
clear value case, explains an *explicit* account prerequisite when the RDS
catalogue supports it, and provides one low-pressure next step.  It never
updates a profile, changes a score, or creates a cross-domain task.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import anyio

from agent1 import storage

from .credit_card_models import CardRecommendation, CreditCardProduct


ADVISOR_POLICY_VERSION = "credit-card-advisor-v2"
FRESHNESS = timedelta(days=30)

# CARD_ELIGIBILITY_RULE.value_text is the authoritative link from a card to an
# account prerequisite.  The account_product_relation table describes account
# to account relationships only, so it must not be inferred as a card link.
# Keeping this small mapping explicit prevents accidental cross-market offers.
ACCOUNT_REQUIREMENT_TO_PRODUCT = {
    "HSBC Premier": ("HK", "hk_premier"),
    "uk_premier_account": ("UK", "uk_premier"),
    "uk_student_account": ("UK", "uk_student"),
    "us_premier_checking": ("US", "us_premier_checking"),
}

SCENARIO_LABELS = {
    "online": "网购", "dining": "餐饮", "travel": "旅行",
    "travel_and_transport": "旅行与交通", "overseas": "境外消费",
    "mainland_china": "内地消费", "mainland_china_and_macau": "内地及澳门消费",
    "retail": "零售消费", "groceries": "日常采购", "general": "日常消费",
    "selected_lifestyle": "指定生活消费类别", "selected_retail": "指定零售类别",
    "dining_and_lifestyle": "餐饮及生活消费", "gas_and_groceries": "加油及日常采购",
    "tuition": "学费",
}
BENEFIT_LABELS = {
    "Instant digital card access": "批核后可即时使用数码卡",
    "Online merchant offers": "网上商户优惠",
    "Mobile wallet support": "支持手机钱包支付",
    "UnionPay QR payments": "支持银联二维码支付",
    "HKD and CNY settlement": "港元及人民币双币结算",
    "Mainland China merchant offers": "内地商户优惠",
    "Travel protection": "旅游保障",
    "Airport lounge access": "机场贵宾室礼遇",
    "Airport lounge or dining access": "机场贵宾室或餐饮礼遇",
    "Priority Pass lounge access": "机场贵宾室礼遇",
}


@dataclass(frozen=True)
class AccountPrerequisite:
    product_name: str
    market: str
    official_url: str
    verified_at: datetime
    card_rule_source_url: str


@dataclass(frozen=True)
class AdvisorBrief:
    steps: list[str] = field(default_factory=list)
    next_action: str | None = None
    evidence_refs: list[str] = field(default_factory=list)
    audit: dict = field(default_factory=dict)


class RdsCardAccountPathRepository:
    """Read active prerequisite accounts in a separate, failure-tolerant query."""

    async def active_account(self, requirement: str, now: datetime) -> AccountPrerequisite | None:
        target = ACCOUNT_REQUIREMENT_TO_PRODUCT.get(requirement)
        if target is None:
            return None
        return await anyio.to_thread.run_sync(self._load, requirement, target, now)

    @staticmethod
    def _load(requirement: str, target: tuple[str, str], now: datetime) -> AccountPrerequisite | None:
        market, product_code = target
        try:
            with storage.read_session() as db:
                row = db.execute(
                    """SELECT product_name, market, official_url, last_verified_at
                       FROM account_product
                       WHERE market=%s AND product_code=%s AND product_status='ACTIVE'""",
                    (market, product_code),
                ).fetchone()
        except Exception:
            return None
        if not row or not row["official_url"] or not row["last_verified_at"]:
            return None
        if now - row["last_verified_at"] > FRESHNESS:
            return None
        # The card-rule URL is filled by the caller from the same verified rule;
        # the repository never fabricates a relationship source.
        return AccountPrerequisite(
            product_name=row["product_name"], market=row["market"],
            official_url=row["official_url"], verified_at=row["last_verified_at"],
            card_rule_source_url="",
        )


class CreditCardAdvisor:
    """Build customer-facing advisory copy without influencing recommendation logic."""

    def __init__(self, account_paths: RdsCardAccountPathRepository | None = None, clock=None):
        self.account_paths = account_paths or RdsCardAccountPathRepository()
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    async def enrich(self, card: CardRecommendation, product: CreditCardProduct,
                     facts: dict, related_cards: list[CardRecommendation] | None = None) -> AdvisorBrief:
        # Unknown or ineligible recommendations must never receive sales copy.
        if card.eligibility.status not in {"eligible", "needs_bank_assessment"}:
            return AdvisorBrief(audit={
                "policy_version": ADVISOR_POLICY_VERSION,
                "suppressed": "eligibility_not_confirmed",
            })

        related_cards = related_cards or []
        steps = self._value_case(card, product, facts)
        usage = self._usage_plan(product, card, facts)
        if usage:
            steps.append("用卡时可以留意这些细节：" + "；".join(usage) + "。")
        prerequisite = await self._verified_prerequisite(product)
        evidence = [card.official_url] if card.official_url else []
        if prerequisite:
            steps.append(
                f"申请 {card.product_name} 前，建议先看看您是否符合 {prerequisite.product_name} "
                "的开户条件和时间安排；信用卡申请仍须由银行审批。"
            )
            evidence.extend([prerequisite.official_url, prerequisite.card_rule_source_url])

        tradeoff = self._tradeoff(card)
        if tradeoff:
            steps.append(tradeoff)
        pairing = self._pairing_case(card, related_cards)
        if pairing:
            steps.append(pairing)

        next_action = (
            f"如果您愿意继续了解，可以先确认 {prerequisite.product_name} 的开户资格与时间安排。"
            if prerequisite else
            "如果您考虑申请，我们可以再一起核对资格、年费豁免条件和奖励上限。"
        )
        steps.append(next_action)
        return AdvisorBrief(
            steps=steps, next_action=next_action,
            evidence_refs=list(dict.fromkeys(item for item in evidence if item)),
            audit={
                "policy_version": ADVISOR_POLICY_VERSION,
                "card_id": card.card_id,
                "scenarios": list(card.matched_scenarios),
                "account_prerequisite": prerequisite.product_name if prerequisite else None,
                "usage_actions": usage,
                "pairing_card_ids": [item.card_id for item in related_cards],
                "ranking_changed": False,
                "profile_written": False,
            },
        )

    async def _verified_prerequisite(self, product: CreditCardProduct) -> AccountPrerequisite | None:
        for rule in product.eligibility_rules:
            if rule.rule_type != "banking_relationship" or not rule.value_text:
                continue
            if not rule.source_url or not rule.last_verified_at:
                continue
            if self.clock() - rule.last_verified_at > FRESHNESS:
                continue
            account = await self.account_paths.active_account(rule.value_text, self.clock())
            if account:
                return AccountPrerequisite(
                    product_name=account.product_name, market=account.market,
                    official_url=account.official_url, verified_at=account.verified_at,
                    card_rule_source_url=rule.source_url,
                )
        return None

    @staticmethod
    def _value_case(card: CardRecommendation, product: CreditCardProduct, facts: dict) -> list[str]:
        scenes = [SCENARIO_LABELS.get(item, "日常消费") for item in card.matched_scenarios]
        scene_text = "、".join(dict.fromkeys(scenes[:3]))
        if scene_text:
            lead = f"您比较在意{scene_text}，这张卡在这些用途上比较贴合；我们也会一起看费用和奖励限制，不只看宣传回赠率。"
        else:
            lead = "按您目前提供的情况，这张卡值得优先比较；实际是否合适，还要看您会怎样使用和持卡成本。"
        lines = [lead]
        # The engine's monetary estimate is HKD-specific.  Never render it as a
        # universal promise for a UK/US card, or when the customer may carry a balance.
        if (product.market == "Hong Kong" and card.estimated_annual_net_value_hkd is not None
                and card.estimated_annual_net_value_hkd > Decimal("0")
                and facts.get("repayment_mode") == "full"):
            parts = []
            if card.estimated_annual_reward_hkd is not None:
                parts.append(f"预计奖赏约 {card.estimated_annual_reward_hkd:,.0f} 港元")
            if card.estimated_annual_fee_hkd is not None:
                parts.append(f"估算年费约 {card.estimated_annual_fee_hkd:,.0f} 港元")
            if card.estimated_annual_fx_cost_hkd is not None and card.estimated_annual_fx_cost_hkd > 0:
                parts.append(f"预计外币手续费约 {card.estimated_annual_fx_cost_hkd:,.0f} 港元")
            parts.append(f"年度净价值约 {card.estimated_annual_net_value_hkd:,.0f} 港元")
            lines.append("按您提供的消费情况粗略估算，" + "；".join(parts) +
                         "。这个估算以每月全额还款为前提，实际回报还会受消费类别、上限和活动条款影响。")
        return lines

    @staticmethod
    def _usage_plan(product: CreditCardProduct, card: CardRecommendation, facts: dict) -> list[str]:
        actions = []
        rules = product.reward_rules
        matched = set(card.matched_scenarios)
        if any(rule.qualification_type == "selected_category" for rule in rules):
            actions.append("如要取得自选类别加速奖赏，先在官方渠道确认并设定合适的消费类别")
        if ("mainland_china" in matched and facts.get("mainland_payment_method") == "mobile_or_qr"
                and any(rule.qualification_type == "payment_method" for rule in rules)):
            actions.append("内地消费尽量按对应条款使用手机或二维码支付")
        benefits = [BENEFIT_LABELS[item.benefit_name] for item in product.benefits
                    if item.benefit_name in BENEFIT_LABELS]
        if benefits:
            actions.append("可重点使用" + "、".join(dict.fromkeys(benefits[:2])))
        return actions[:3]

    @staticmethod
    def _pairing_case(card: CardRecommendation, related_cards: list[CardRecommendation]) -> str | None:
        supplement = next((item for item in related_cards if item.role == "supplement"), None)
        if supplement is None:
            return None
        primary_scenes = set(card.matched_scenarios)
        supplement_scenes = [SCENARIO_LABELS.get(item, "指定消费")
                              for item in supplement.matched_scenarios
                              if item not in primary_scenes]
        if supplement_scenes:
            return (f"{card.product_name} 作为主卡时，{supplement.product_name} 可补足"
                    f"{'、'.join(dict.fromkeys(supplement_scenes[:2]))}；是否双卡应先比较总年费和奖励上限。")
        return (f"如考虑同时持有 {supplement.product_name}，请先比较两张卡的年费、"
                "共同奖励上限和您实际会不会使用对应权益。")

    @staticmethod
    def _tradeoff(card: CardRecommendation) -> str | None:
        if card.estimated_annual_fee_hkd and card.estimated_annual_fee_hkd > 0:
            return "若您不常使用核心权益或未能满足年费豁免条件，应与免年费选择一并比较。"
        if card.foreign_transaction_fee_rate and card.foreign_transaction_fee_rate > 0:
            return "若主要用于非目标市场的外币消费，应一并比较外币手续费，而不只看回赠。"
        return "如果您的主要消费场景改变，建议重新比较，而不是仅因当前回赠率办卡。"
