"""Load current HSBC HK credit-card borrowing terms into the shared RDS catalog.

The values below come from HSBC's linked Credit Card and Cash Instalment Plan
Key Facts Statements and its current Spending Instalment Plan page.  This loader
keeps each value's source and verification time beside the value.
"""

from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agent1 import storage


CREDIT_CARD_KFS = (
    "https://www.hsbc.com.hk/content/dam/hsbc/hk/docs/credit-cards/"
    "key-fact-statement.pdf"
)
CASH_INSTALMENT_KFS = (
    "https://www.hsbc.com.hk/content/dam/hsbc/hk/docs/credit-cards/"
    "cash-instalment-plan/key-facts-statement.pdf"
)
CASH_INSTALMENT_PAGE = (
    "https://www.hsbc.com.hk/credit-cards/use-your-card/cash-instalment-plan/"
)
SPENDING_INSTALMENT_PAGE = (
    "https://www.hsbc.com.hk/credit-cards/use-your-card/spending-instalment/"
)

CASH_EXAMPLES = (
    # tenor, APR, monthly repayment, total repayment; example principal HKD100,000
    (6, Decimal("0.1848"), Decimal("17506.67"), Decimal("105040")),
    (12, Decimal("0.1969"), Decimal("9173.34"), Decimal("110080")),
    (24, Decimal("0.1991"), Decimal("5006.67"), Decimal("120160")),
)


def _base_terms(card_id, verified_at):
    return [
        (f"{card_id}:purchase-apr", card_id, "purchase_apr", Decimal("0.3542"), None,
         None, None, None, None, None, None, True, False,
         "开户时零售签账 APR；每月到期日前全额还清结单余额则不收利息。",
         CREDIT_CARD_KFS, date(2025, 6, 1), verified_at),
        (f"{card_id}:cash-advance-apr", card_id, "cash_advance_apr", Decimal("0.3594"),
         None, None, None, None, None, None, None, True, False,
         "开户时现金透支 APR，已包含现金透支及手续费。",
         CREDIT_CARD_KFS, date(2025, 6, 1), verified_at),
        (f"{card_id}:interest-free", card_id, "interest_free_period", None, None,
         None, 56, None, None, None, None, True, False,
         "最长免息期为 56 天；是否产生利息取决于按时全额还款等条款。",
         CREDIT_CARD_KFS, date(2025, 6, 1), verified_at),
    ]


def _instalment_terms(card_id, verified_at):
    if card_id == "hsbc_visa_gold_student":
        unavailable = "HSBC 条款明确排除 undergraduate credit card。"
        return [
            (f"{card_id}:cash-instalment-offer-unavailable", card_id,
             "cash_instalment_offer", None, None, None, None, None, None, None, None,
             False, True, unavailable, CASH_INSTALMENT_PAGE, None, verified_at),
            (f"{card_id}:cash-instalment-unavailable", card_id, "cash_instalment_example",
             None, None, None, None, None, None, None, None, False, True,
             unavailable, CASH_INSTALMENT_KFS, date(2026, 6, 9), verified_at),
            (f"{card_id}:spending-instalment-unavailable", card_id, "spending_instalment",
             None, None, None, None, None, None, None, None, False, True,
             unavailable, SPENDING_INSTALMENT_PAGE, date(2024, 10, 1), verified_at),
        ]

    cash = [(
        f"{card_id}:cash-instalment-offer", card_id, "cash_instalment_offer",
        None, Decimal("0.0019"), None, None, Decimal("2000"), None, None, "HKD",
        True, True,
        "当前页面发布每月手续费低至 0.19%，最低贷款额 HKD2,000；实际报价和获批 APR 因客户而异。",
        CASH_INSTALMENT_PAGE, None, verified_at,
    )]
    for tenor, apr, periodic, total in CASH_EXAMPLES:
        cash.append((
            f"{card_id}:cash-instalment:{tenor}", card_id, "cash_instalment_example",
            apr, Decimal("0.0084"), tenor, None, Decimal("100000"), periodic, total,
            "HKD", True, True,
            "KFS 的 HKD100,000 示例；0.84% 为该示例每月手续费率，实际获批 APR 和报价可能不同。",
            CASH_INSTALMENT_KFS, date(2026, 6, 9), verified_at,
        ))
    cash.append((
        f"{card_id}:spending-instalment", card_id, "spending_instalment",
        None, None, None, None, Decimal("1000"), None, None, "HKD", True, True,
        "可选 6 至 60 个月；最低申请额 HKD1,000。按时还款不收利息，但手续费为客户级报价，须在 HSBC HK App、Reward+ 或热线查询。",
        SPENDING_INSTALMENT_PAGE, date(2024, 10, 1), verified_at,
    ))
    return cash


def sync():
    migration = (Path(__file__).resolve().parents[1] / "agent1" / "migrations" /
                 "009_credit_card_borrowing_terms.sql").read_text(encoding="utf-8")
    verified_at = datetime.now(timezone.utc)
    connection = storage._connect()
    try:
        connection.execute(migration)
        cards = connection.execute(
            "SELECT card_id FROM CREDIT_CARD "
            "WHERE issuing_bank='HSBC' AND market='Hong Kong' AND product_status='ACTIVE' "
            "ORDER BY card_id"
        ).fetchall()
        card_ids = [row["card_id"] for row in cards]
        if not card_ids:
            raise RuntimeError("No active HSBC Hong Kong credit cards found")

        connection.execute(
            "DELETE FROM CARD_BORROWING_TERM WHERE card_id = ANY(%s)", (card_ids,))
        rows = []
        for card_id in card_ids:
            rows.extend(_base_terms(card_id, verified_at))
            rows.extend(_instalment_terms(card_id, verified_at))
        with connection.cursor() as cursor:
            cursor.executemany(
                """INSERT INTO CARD_BORROWING_TERM (
                borrowing_term_id, card_id, term_type, apr, monthly_fee_rate,
                tenor_months, max_interest_free_days, example_principal,
                periodic_repayment, total_repayment, currency, available,
                personalized_pricing, description, source_url,
                source_effective_date, last_verified_at
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                rows,
            )
        connection.commit()
        return {"cards": len(card_ids), "terms": len(rows), "verified_at": verified_at.isoformat()}
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


if __name__ == "__main__":
    print(sync())
