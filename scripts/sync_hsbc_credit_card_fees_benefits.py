"""Replace demo fee metadata with sourced HSBC HK terms and source benefits."""

from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agent1 import storage
from scripts.sync_hsbc_credit_card_eligibility import PRODUCT_URLS


FEE_SCHEDULE_URL = "https://www.hsbc.com.hk/content/dam/hsbc/hk/docs/credit-cards/red/ewelcome-pack.pdf"
FX_SOURCE_URL = "https://www.hsbc.com.hk/help/faq/credit-cards/"

ANNUAL_FEES = {
    "hsbc_easy": (Decimal("1800"), "First two years waived"),
    "hsbc_everymile": (Decimal("2000"),
                       "First year waived; subsequent years waived with "
                       "HKD80,000 annual eligible spend"),
    "hsbc_premier_mastercard": (Decimal("0"), "Perpetual annual fee waiver"),
    "hsbc_pulse_dual_currency": (Decimal("1800"), "First two years waived"),
    "hsbc_red": (Decimal("0"), "Perpetual annual fee waiver"),
    "hsbc_unionpay_dual_currency": (Decimal("300"), "First two years waived"),
    "hsbc_visa_gold": (Decimal("600"), "First two years waived"),
    "hsbc_visa_gold_student": (Decimal("0"), "Perpetual annual fee waiver"),
    "hsbc_visa_signature": (Decimal("2000"), "First two years waived"),
}

# Cards whose annual-fee waiver depends on a minimum annual eligible spend.
FEE_WAIVER_MIN_SPEND_HKD = {
    "hsbc_everymile": Decimal("80000"),
}


def sync():
    verified_at = datetime.now(timezone.utc)
    root = Path(__file__).resolve().parents[1]
    migration = (root / "agent1" / "migrations" /
                 "014_credit_card_reward_stacking.sql").read_text(encoding="utf-8")
    connection = storage._connect()
    try:
        connection.execute(migration)
        for card_id, (amount, waiver) in ANNUAL_FEES.items():
            connection.execute(
                """UPDATE CARD_FEE SET amount=%s,currency='HKD',waiver_condition=%s,
                   notes=NULL,scope_text='primary card annual fee',source_url=%s,last_verified_at=%s,
                   waiver_min_annual_spend_hkd=%s
                   WHERE card_id=%s AND fee_type='annual_fee'""",
                (amount, waiver, FEE_SCHEDULE_URL, verified_at,
                 FEE_WAIVER_MIN_SPEND_HKD.get(card_id), card_id),
            )
            connection.execute(
                """UPDATE CARD_FEE SET rate=%s,notes=NULL,scope_text=%s,
                   source_url=%s,last_verified_at=%s
                   WHERE card_id=%s AND fee_type='foreign_transaction_fee'""",
                (Decimal("0.01") if "dual_currency" in card_id else Decimal("0.0195"),
                 ("1% for currencies other than RMB/HKD/MOP; RMB and MOP are waived"
                  if card_id == "hsbc_pulse_dual_currency" else
                  "1% for currencies other than RMB/HKD; RMB is waived")
                 if "dual_currency" in card_id else "Transactions effected in currencies other than HKD",
                 FX_SOURCE_URL, verified_at, card_id),
            )
            connection.execute(
                "UPDATE CARD_BENEFIT SET source_url=%s,last_verified_at=%s WHERE card_id=%s",
                (PRODUCT_URLS[card_id], verified_at, card_id),
            )
        # The official comparison page lists no travel privilege for Visa Gold.
        connection.execute("DELETE FROM CARD_BENEFIT WHERE benefit_id='benefit_gold_travel'")
        connection.commit()
        return {"cards": len(ANNUAL_FEES), "verified_at": verified_at.isoformat()}
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


if __name__ == "__main__":
    print(sync())
