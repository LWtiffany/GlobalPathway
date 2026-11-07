"""Grounded answers for general, non-product-specific credit-card questions."""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

import anyio

from agent1 import storage

from agent2.models import AnswerBody, Notice

from ..contracts import DomainResult, DomainTask, OrchestrationInput

KNOWLEDGE_VERSION = "credit-card-knowledge-v1"
FRESHNESS = timedelta(days=90)
MARKET_MAP = {"CN-HK": "CN-HK", "Hong Kong": "CN-HK", "HK": "CN-HK",
              "UK": "UK", "US": "US"}

TOPIC_PATTERNS = {
    "repayment": re.compile(r"还款|还清|最低还款|账单日|还款日|利息|finance.?charge", re.I),
    "rewards": re.compile(r"积分|奖赏钱|rewardcash|回赠.*兑换|里程.*兑换|积分.*兑换", re.I),
    "overseas_use": re.compile(r"外币|海外|境外|出国.{0,12}刷卡|汇率|换汇|动态货币|dcc|"
                               r"(?:港币|港元|当地|本地|原币).{0,10}(?:结算|付款货币)|"
                               r"(?:结算|付款货币).{0,10}(?:港币|港元|当地|本地|原币)", re.I),
    "lost_or_stolen": re.compile(r"丢[了失]|遗失|被盗|盗刷|锁卡|挂失|卡不见", re.I),
    "transaction_dispute": re.compile(r"争议|不认识.*交易|重复扣款|未收到.*商品|拒付|chargeback", re.I),
    "instalment": re.compile(r"分期|最低还款|循环利息|现金透支|总还款", re.I),
}


class RdsCreditCardKnowledge:
    async def read(self, market: str, topic: str, now: datetime) -> dict | None:
        return await anyio.to_thread.run_sync(self._load, market, topic, now)

    @staticmethod
    def _load(market: str, topic: str, now: datetime) -> dict | None:
        try:
            with storage.read_session() as db:
                rows = db.execute(
                    """SELECT knowledge_id, market, topic, title, customer_answer, source_url, verified_at
                       FROM card_knowledge_entry
                       WHERE status='ACTIVE' AND topic=%s AND market IN (%s, 'GENERAL')
                         AND (effective_from IS NULL OR effective_from <= %s)
                         AND (effective_to IS NULL OR effective_to >= %s)
                       ORDER BY CASE WHEN market=%s THEN 0 ELSE 1 END, verified_at DESC""",
                    (topic, market, now.date(), now.date(), market),
                ).fetchall()
        except Exception:
            return None
        for row in rows:
            if row["verified_at"] and now - row["verified_at"] <= FRESHNESS:
                return dict(row)
        return None


class CreditCardKnowledgeService:
    def __init__(self, repository: RdsCreditCardKnowledge | None = None, clock=None):
        self.repository = repository or RdsCreditCardKnowledge()
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    async def answer(self, task: DomainTask, request: OrchestrationInput,
                     facts: dict, extracted: dict, basis: dict) -> DomainResult | None:
        topic = self._topic(request.text)
        if topic is None:
            return None
        market = MARKET_MAP.get(facts.get("application_region"), "CN-HK")
        item = await self.repository.read(market, topic, self.clock())
        if item is None:
            return DomainResult(
                task_id=task.task_id, domain="credit_card", status="needs_verification",
                answer=AnswerBody(summary="这项信用卡使用问题目前缺少可核对的最新资料。",
                                  steps=["请先核对发卡行的该卡条款；如告诉我卡名和发卡地区，我可以继续查具体规则。"]),
                payload={"capability": "general_knowledge", "topic": topic, "market": market},
                projection="chat_only", observed_facts=extracted, observed_basis=basis,
            )
        market_label = {"CN-HK": "香港", "UK": "英国", "US": "美国"}.get(item["market"], "通用")
        return DomainResult(
            task_id=task.task_id,
            domain="credit_card",
            status="answered",
            answer=AnswerBody(
                summary=item["title"],
                steps=[item["customer_answer"]],
                limitations=[
                    f"以上为{market_label}信用卡通用说明；具体费用、期限和处理结果以您的卡片条款、账单及银行核实为准。"
                ],
            ),
            evidence_refs=[item["source_url"]],
            next_actions=[self._next_action(topic)],
            notices=[Notice(code="credit_card_knowledge_grounded",
                            message="回答基于已核验的信用卡通用知识")],
            payload={"capability": "general_knowledge", "topic": topic,
                     "knowledge_id": item["knowledge_id"], "market": item["market"],
                     "knowledge_version": KNOWLEDGE_VERSION},
            projection="chat_only",
            observed_facts=extracted,
            observed_basis=basis,
            knowledge_read_id=item["knowledge_id"],
        )

    @staticmethod
    def _topic(text: str) -> str | None:
        matches = [(match.start(), topic) for topic, pattern in TOPIC_PATTERNS.items()
                   if (match := pattern.search(text))]
        return min(matches, default=(None, None))[1]

    @staticmethod
    def _next_action(topic: str) -> str:
        return {
            "repayment": "如您愿意，我可以继续按您持有的具体卡片和账单金额说明需要核对哪些项目。",
            "rewards": "如您说明持有的卡和想兑换的用途，我可以继续核对适用规则。",
            "overseas_use": "如您提供目的地和具体卡种，我可以继续比较该卡的外币费用与适用方式。",
            "lost_or_stolen": "如您现在正遇到遗失或未授权交易，请优先锁卡／联系银行，再保留相关记录。",
            "transaction_dispute": "如您提供交易状态和争议原因，我可以帮您整理提交争议前应准备的资料。",
            "instalment": "如您提供卡种、金额和期数，我可以帮您列出应比较的总成本项目。",
        }[topic]
