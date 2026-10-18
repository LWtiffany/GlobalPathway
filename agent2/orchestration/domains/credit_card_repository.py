"""Read-only adapter for credit-card product tables in the shared RDS database."""

import uuid
from collections import defaultdict
from datetime import datetime
from typing import Protocol

import anyio

from agent1 import storage

from .credit_card_models import (CardBenefit, CardCatalogRead, CardFee, CreditCardProduct,
                                 EligibilityRule, RewardRule)


class CreditCardRepository(Protocol):
    async def active_products(self, market: str, now: datetime) -> CardCatalogRead: ...


class CreditCardRepositoryError(RuntimeError):
    pass


MARKET_TO_RDS = {"CN-HK": "Hong Kong", "HK": "Hong Kong", "Hong Kong": "Hong Kong",
                 "UK": "UK", "US": "US", "CN": "China"}


class RdsCreditCardRepository:
    """Uses agent1.storage so local SSH tunnel and production RDS settings stay shared."""

    async def active_products(self, market: str, now: datetime) -> CardCatalogRead:
        return await anyio.to_thread.run_sync(self._load, market, now)

    def _load(self, market: str, now: datetime) -> CardCatalogRead:
        rds_market = MARKET_TO_RDS.get(market, market)
        try:
            with storage.read_session() as db:
                rows = db.execute(
                    """SELECT * FROM CREDIT_CARD
                       WHERE product_status='ACTIVE' AND market=%s
                         AND (effective_from IS NULL OR effective_from <= %s)
                         AND (effective_to IS NULL OR effective_to >= %s)
                       ORDER BY issuing_bank, product_name""",
                    (rds_market, now.date(), now.date()),
                ).fetchall()
                ids = [row["card_id"] for row in rows]
                children = self._children(db, ids)
        except Exception as exc:  # keep infrastructure failures inside the domain boundary
            raise CreditCardRepositoryError(str(exc)) from exc

        products = [CreditCardProduct(
            card_id=row["card_id"], issuing_bank=row["issuing_bank"], card_network=row["card_network"],
            market=row["market"], product_name=row["product_name"], card_tier=row["card_tier"],
            target_audience=row["target_audience"], official_url=row["official_url"],
            data_status=row["data_status"], last_verified_at=row["last_verified_at"],
            eligibility_rules=children["eligibility"].get(row["card_id"], []),
            reward_rules=children["rewards"].get(row["card_id"], []),
            fees=children["fees"].get(row["card_id"], []),
            benefits=children["benefits"].get(row["card_id"], []),
        ) for row in rows]
        return CardCatalogRead(read_id=f"rds-credit-card-{uuid.uuid4()}", market=rds_market,
                               products=products)

    @staticmethod
    def _children(db, ids):
        result = {name: defaultdict(list) for name in ("eligibility", "rewards", "fees", "benefits")}
        if not ids:
            return result
        for row in db.execute("SELECT * FROM CARD_ELIGIBILITY_RULE WHERE card_id = ANY(%s)", (ids,)):
            result["eligibility"][row["card_id"]].append(EligibilityRule(
                rule_id=row["eligibility_rule_id"], rule_type=row["rule_type"], operator=row["operator"],
                value_text=row["value_text"], value_numeric=row["value_numeric"], currency=row["currency"],
                required=bool(row["required"]), description=row["description"]))
        for row in db.execute("SELECT * FROM CARD_REWARD_RULE WHERE card_id = ANY(%s)", (ids,)):
            result["rewards"][row["card_id"]].append(RewardRule(
                rule_id=row["reward_rule_id"], spend_category=row["spend_category"],
                reward_type=row["reward_type"], reward_rate=row["reward_rate"],
                points_per_currency_unit=row["points_per_currency_unit"], spend_cap=row["spend_cap"],
                cap_currency=row["cap_currency"], cap_period=row["cap_period"], description=row["description"]))
        for row in db.execute("SELECT * FROM CARD_FEE WHERE card_id = ANY(%s)", (ids,)):
            result["fees"][row["card_id"]].append(CardFee(
                fee_id=row["fee_id"], fee_type=row["fee_type"], amount=row["amount"],
                currency=row["currency"], rate=row["rate"], waiver_condition=row["waiver_condition"],
                notes=row["notes"]))
        for row in db.execute("SELECT * FROM CARD_BENEFIT WHERE card_id = ANY(%s)", (ids,)):
            result["benefits"][row["card_id"]].append(CardBenefit(
                benefit_id=row["benefit_id"], benefit_type=row["benefit_type"],
                benefit_name=row["benefit_name"], quantity=row["quantity"], period=row["period"],
                description=row["description"]))
        return result
