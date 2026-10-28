"""Add sourced cash-equivalent rates and layered (stacked) reward semantics.

Reward modelling (2026-10 HSBC HK terms):
- Cards earn a base rate (1X RewardCash = 0.4%) on all spending; EveryMile's
  base is 1.0% RewardCash-equivalent via mile conversion.
- "Rewards of Your Choice" contributes an extra 5X = 2.0% layer on one selected
  category, capped at the first HKD100,000 eligible spend per calendar year of
  the programme (shared across cards per HKID).
- Pulse's mainland QR/mobile extra 2.0% is capped at the first HKD80,000
  eligible spend per promotional period (official T&C, per HKID), promotional.
- Visa Signature's extra 1.2% applies automatically on all Rewards of Your
  Choice categories until 31 December 2026 (promotional, no amount cap).
- Red's online 4% / designated-merchant 8% are total rates with monthly caps.
"""

from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agent1 import storage
from scripts.sync_hsbc_credit_card_eligibility import PRODUCT_URLS

PROMO_UNTIL = datetime(2026, 12, 31, 15, 59, 59, tzinfo=timezone.utc)  # 2026-12-31 23:59:59 HKT

EASY_EQUIVALENTS = {
    "reward_easy_base": Decimal("0.004"),
    "reward_easy_retail": Decimal("0.024"),
}

# Rewards of Your Choice extra layer: 2.4% total = 0.4% base + 2.0% extra,
# capped at first HKD100,000 eligible spend per programme year (per HKID).
YC_RULE_IDS = [
    "reward_premier_travel",
    "reward_unionpay_crossborder",
    "reward_gold_overseas",
    "reward_pulse_lifestyle",
    "reward_signature_lifestyle",
]
YC_EXTRA_RATE = Decimal("0.020")
YC_CAP = (Decimal("100000"), "HKD", "year")

# Pulse mainland QR/mobile payment layer: extra 2.0% on top of base + Your Choice,
# capped at first HKD80,000 eligible spend per promotional period (official T&C).
PULSE_QR_RULE_ID = "reward_pulse_mainland"
PULSE_QR_EXTRA_RATE = Decimal("0.020")
PULSE_QR_CAP = (Decimal("80000"), "HKD", "year")

# Visa Signature automatic extra 1.2% on all Rewards of Your Choice
# categories, promotional until 31 December 2026, no amount cap.
SIGNATURE_PROMO_RULE = (
    "reward_signature_promo", "hsbc_visa_signature", "selected_lifestyle",
    "cashback", Decimal("0.012"), "selected_category", None,
    "Extra 1.2% RewardCash automatically on all Rewards of Your Choice "
    "categories (promotional, until 31 December 2026)",
)

NEW_RULES = [
    SIGNATURE_PROMO_RULE,
    ("reward_pulse_lifestyle", "hsbc_pulse_dual_currency", "selected_lifestyle",
     "cashback", YC_EXTRA_RATE, "selected_category", None,
     "Extra 2.0% RewardCash in one selected category (Rewards of Your Choice, "
     "first HKD100,000 eligible spend per programme year)"),
    ("reward_red_selected_merchants", "hsbc_red", "selected_retail",
     "cashback", Decimal("0.08"), "designated_merchant", "red_named_merchants",
     "8% RewardCash on the first HKD1,250 at designated merchants each month"),
]


def sync():
    verified_at = datetime.now(timezone.utc)
    root = Path(__file__).resolve().parents[1]
    migrations = root / "agent1" / "migrations"
    connection = storage._connect()
    try:
        for name in ("011_credit_card_reward_value.sql",
                     "013_credit_card_reward_qualifiers.sql",
                     "014_credit_card_reward_stacking.sql"):
            connection.execute((migrations / name).read_text(encoding="utf-8"))
        connection.execute(
            "UPDATE CARD_REWARD_RULE SET cash_equivalent_rate=reward_rate, stacking='total' "
            "WHERE reward_type='cashback' AND stacking IS DISTINCT FROM 'total'"
        )
        for rule_id, rate in EASY_EQUIVALENTS.items():
            connection.execute(
                "UPDATE CARD_REWARD_RULE SET cash_equivalent_rate=%s, stacking='total' "
                "WHERE reward_rule_id=%s",
                (rate, rule_id),
            )
        # EveryMile cash equivalents: designated 2.5% / general 1.0% RewardCash.
        connection.execute(
            "UPDATE CARD_REWARD_RULE SET cash_equivalent_rate=0.025, stacking='total' "
            "WHERE card_id='hsbc_everymile' AND spend_category='travel_and_transport'"
        )
        connection.execute(
            "UPDATE CARD_REWARD_RULE SET cash_equivalent_rate=0.010, stacking='total' "
            "WHERE card_id='hsbc_everymile' AND spend_category='general'"
        )
        # Rewards of Your Choice: extra layer with the programme-year cap.
        for rule_id in YC_RULE_IDS:
            connection.execute(
                """UPDATE CARD_REWARD_RULE SET spend_category='selected_lifestyle',
                   reward_type='cashback', reward_rate=%s, points_per_currency_unit=NULL,
                   cash_equivalent_rate=%s, spend_cap=%s, cap_currency=%s, cap_period=%s,
                   stacking='extra', valid_until=NULL, qualification_type='selected_category',
                   qualification_value=NULL,
                   description='Extra 2.0%% RewardCash in one selected category (Rewards of '
                               'Your Choice, first HKD100,000 eligible spend per programme year)'
                   WHERE reward_rule_id=%s""",
                (YC_EXTRA_RATE, YC_EXTRA_RATE, *YC_CAP, rule_id),
            )
        # Pulse mainland QR/mobile extra layer (official T&C cap + promo window).
        connection.execute(
            """UPDATE CARD_REWARD_RULE SET spend_category='mainland_china_and_macau',
               reward_type='cashback', reward_rate=%s, points_per_currency_unit=NULL,
               cash_equivalent_rate=%s, spend_cap=%s, cap_currency=%s, cap_period=%s,
               stacking='extra', valid_until=%s, qualification_type='payment_method',
               qualification_value='mobile_or_qr',
               description='Extra 2.0%% RewardCash for QR/mobile payments in Mainland China and '
                           'Macau in RMB/MOP (promotional; first HKD80,000 eligible spend per '
                           'promotional period, per HKID, until 31 December 2026)'
               WHERE reward_rule_id=%s""",
            (PULSE_QR_EXTRA_RATE, PULSE_QR_EXTRA_RATE, *PULSE_QR_CAP, PROMO_UNTIL,
             PULSE_QR_RULE_ID),
        )
        # Red online: total rate with monthly cap.
        connection.execute(
            """UPDATE CARD_REWARD_RULE SET spend_cap=10000,cap_currency='HKD',cap_period='month',
               stacking='total', valid_until=NULL,
               qualification_type='automatic',qualification_value=NULL
               WHERE reward_rule_id='reward_red_online'"""
        )
        # Red designated merchants: total rate, keep monthly cap, not stacked.
        connection.execute(
            """UPDATE CARD_REWARD_RULE SET stacking='total', valid_until=NULL
               WHERE reward_rule_id='reward_red_selected_merchants'"""
        )
        for rule_id, card_id, category, reward_type, rate, qualification_type, \
                qualification_value, description in NEW_RULES:
            is_promo = rule_id == SIGNATURE_PROMO_RULE[0]
            is_yc = rule_id == "reward_pulse_lifestyle"
            is_retail = rule_id == "reward_red_selected_merchants"
            connection.execute(
                """INSERT INTO CARD_REWARD_RULE (
                   reward_rule_id,card_id,spend_category,reward_type,reward_rate,
                   cash_equivalent_rate,qualification_type,qualification_value,description,
                   source_url,last_verified_at,spend_cap,cap_currency,cap_period,
                   stacking,valid_until
                   ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT (reward_rule_id) DO UPDATE SET
                   spend_category=EXCLUDED.spend_category,reward_type=EXCLUDED.reward_type,
                   reward_rate=EXCLUDED.reward_rate,cash_equivalent_rate=EXCLUDED.cash_equivalent_rate,
                   qualification_type=EXCLUDED.qualification_type,
                   qualification_value=EXCLUDED.qualification_value,
                   description=EXCLUDED.description,source_url=EXCLUDED.source_url,
                   last_verified_at=EXCLUDED.last_verified_at,spend_cap=EXCLUDED.spend_cap,
                   cap_currency=EXCLUDED.cap_currency,cap_period=EXCLUDED.cap_period,
                   stacking=EXCLUDED.stacking,valid_until=EXCLUDED.valid_until""",
                (rule_id, card_id, category, reward_type, rate, rate, qualification_type,
                 qualification_value, description, PRODUCT_URLS[card_id], verified_at,
                 (YC_CAP[0] if is_yc else
                  Decimal("1250") if is_retail else None),
                 ("HKD" if (is_yc or is_retail) else None),
                 ("year" if is_yc else "month" if is_retail else None),
                 ("extra" if (is_promo or is_yc) else "total"),
                 (PROMO_UNTIL if is_promo else None)),
            )
        for card_id, source_url in PRODUCT_URLS.items():
            connection.execute(
                "UPDATE CARD_REWARD_RULE SET source_url=%s,last_verified_at=%s WHERE card_id=%s",
                (source_url, verified_at, card_id),
            )
            connection.execute(
                "UPDATE CREDIT_CARD SET last_verified_at=%s, official_url=%s WHERE card_id=%s",
                (verified_at, source_url, card_id),
            )
        connection.commit()
        return {"cards": len(PRODUCT_URLS), "verified_at": verified_at.isoformat()}
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


if __name__ == "__main__":
    print(sync())
