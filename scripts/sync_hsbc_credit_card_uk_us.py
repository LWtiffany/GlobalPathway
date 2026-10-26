"""Seed HSBC UK and US credit-card product rows into the shared RDS catalog.

Official product pages (verified_at stamped at sync time):
  US Premier  https://www.us.hsbc.com/credit-cards/products/premier/
  US Elite    https://www.us.hsbc.com/credit-cards/products/elite/
  UK Student  https://www.hsbc.co.uk/credit-cards/products/student/
  UK Premier  https://www.hsbc.co.uk/credit-cards/products/premier/
  UK World Elite https://www.hsbc.co.uk/credit-cards/products/premier-world-elite/

Idempotent: deletes then re-inserts the five card_id trees below.
"""

from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agent1 import storage

CARD_IDS = (
    "hsbc_us_premier_world",
    "hsbc_us_elite",
    "hsbc_uk_student",
    "hsbc_uk_premier",
    "hsbc_uk_premier_world_elite",
)

US_PREMIER_URL = "https://www.us.hsbc.com/credit-cards/products/premier/"
US_ELITE_URL = "https://www.us.hsbc.com/credit-cards/products/elite/"
UK_STUDENT_URL = "https://www.hsbc.co.uk/credit-cards/products/student/"
UK_PREMIER_URL = "https://www.hsbc.co.uk/credit-cards/products/premier/"
UK_WORLD_ELITE_URL = "https://www.hsbc.co.uk/credit-cards/products/premier-world-elite/"


def sync():
    verified_at = datetime.now(timezone.utc)
    connection = storage._connect()
    try:
        connection.execute(
            "DELETE FROM CREDIT_CARD WHERE card_id = ANY(%s)",
            (list(CARD_IDS),),
        )

        cards = [
            ("hsbc_us_premier_world", "Mastercard", "US",
             "HSBC Premier World Mastercard", "World",
             "U.S. HSBC Premier checking customers", US_PREMIER_URL),
            ("hsbc_us_elite", "Mastercard", "US",
             "HSBC Elite World Elite Mastercard", "World Elite",
             "U.S. HSBC Premier checking customers seeking travel rewards", US_ELITE_URL),
            ("hsbc_uk_student", "Mastercard", "UK",
             "HSBC Student Credit Card", "Student",
             "Existing HSBC UK Student Bank Account holders", UK_STUDENT_URL),
            ("hsbc_uk_premier", "Mastercard", "UK",
             "HSBC Premier Credit Card", "Premier",
             "Existing HSBC UK Premier Account holders", UK_PREMIER_URL),
            ("hsbc_uk_premier_world_elite", "Mastercard", "UK",
             "HSBC Premier World Elite Mastercard", "World Elite",
             "Existing HSBC UK Premier or Private Banking customers", UK_WORLD_ELITE_URL),
        ]
        for row in cards:
            connection.execute(
                """INSERT INTO CREDIT_CARD
                   (card_id, issuing_bank, card_network, market, product_name, card_tier,
                    product_status, target_audience, official_url, last_verified_at)
                   VALUES (%s, 'HSBC', %s, %s, %s, %s, 'ACTIVE', %s, %s, %s)""",
                (*row, verified_at),
            )

        fees = [
            # US Premier: $95 annual, 0% FX
            ("fee_us_premier_annual", "hsbc_us_premier_world", "annual_fee",
             Decimal("95"), "USD", None,
             "Waived for Private Banking relationship or HSBC US employee status",
             "primary card annual fee", US_PREMIER_URL),
            ("fee_us_premier_fx", "hsbc_us_premier_world", "foreign_transaction_fee",
             None, None, Decimal("0"), None,
             "No foreign transaction fee", US_PREMIER_URL),
            # US Elite: $495 annual, 0% FX
            ("fee_us_elite_annual", "hsbc_us_elite", "annual_fee",
             Decimal("495"), "USD", None,
             "Waived for qualifying U.S. HSBC Jade or Private Banking relationship",
             "primary card annual fee", US_ELITE_URL),
            ("fee_us_elite_fx", "hsbc_us_elite", "foreign_transaction_fee",
             None, None, Decimal("0"), None,
             "No foreign transaction fee", US_ELITE_URL),
            # UK Student: £0, FX not highlighted as a product selling point — leave
            # foreign fee unspecified rather than invent a rate.
            ("fee_uk_student_annual", "hsbc_uk_student", "annual_fee",
             Decimal("0"), "GBP", None, "No annual fee",
             "primary card annual fee", UK_STUDENT_URL),
            # UK Premier: £0, 2.99% non-sterling
            ("fee_uk_premier_annual", "hsbc_uk_premier", "annual_fee",
             Decimal("0"), "GBP", None, "No annual fee",
             "primary card annual fee", UK_PREMIER_URL),
            ("fee_uk_premier_fx", "hsbc_uk_premier", "foreign_transaction_fee",
             None, None, Decimal("0.0299"), None,
             "Non-sterling transaction fee 2.99%", UK_PREMIER_URL),
            # UK World Elite: £290, 2.99% non-sterling
            ("fee_uk_we_annual", "hsbc_uk_premier_world_elite", "annual_fee",
             Decimal("290"), "GBP", None, None,
             "primary card annual fee", UK_WORLD_ELITE_URL),
            ("fee_uk_we_fx", "hsbc_uk_premier_world_elite", "foreign_transaction_fee",
             None, None, Decimal("0.0299"), None,
             "Non-sterling transaction fee 2.99%", UK_WORLD_ELITE_URL),
        ]
        for fee_id, card_id, fee_type, amount, currency, rate, waiver, scope, url in fees:
            connection.execute(
                """INSERT INTO CARD_FEE
                   (fee_id, card_id, fee_type, amount, currency, rate, waiver_condition,
                    scope_text, source_url, last_verified_at)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (fee_id, card_id, fee_type, amount, currency, rate, waiver, scope, url,
                 verified_at),
            )

        benefits = [
            ("benefit_us_premier_screening", "hsbc_us_premier_world", "other",
             "Airport screening statement credit (up to $85 / 4.5 years)", None, None,
             "Global Entry, TSA PreCheck, CLEAR, or NEXUS application fee credit",
             US_PREMIER_URL),
            ("benefit_us_premier_streaming", "hsbc_us_premier_world", "other",
             "Streaming statement credit (up to $60 / year)", Decimal("60"), "year",
             "TV and radio streaming subscription credit", US_PREMIER_URL),
            ("benefit_us_premier_points", "hsbc_us_premier_world", "other",
             "Rewards points with airline and hotel transfer partners", None, None,
             "3× gas/groceries, 2× travel, 1× other purchases", US_PREMIER_URL),
            ("benefit_us_elite_lounge", "hsbc_us_elite", "airport_lounge",
             "Priority Pass lounge access", None, None,
             "Cardholder plus up to 2 guests", US_ELITE_URL),
            ("benefit_us_elite_travel_credit", "hsbc_us_elite", "other",
             "HSBC Travel credit ($400 / year)", Decimal("400"), "year",
             "Travel booked via HSBC Travel", US_ELITE_URL),
            ("benefit_us_elite_screening", "hsbc_us_elite", "other",
             "Airport screening statement credit (up to $120 / 4.5 years)", None, None,
             "Global Entry, TSA PreCheck, CLEAR, or NEXUS application fee credit",
             US_ELITE_URL),
            ("benefit_uk_student_support", "hsbc_uk_student", "other",
             "Lost/stolen card support and emergency cash advance", None, None,
             "Dedicated support for existing Student Bank Account holders",
             UK_STUDENT_URL),
            ("benefit_uk_premier_lounge", "hsbc_uk_premier", "airport_lounge",
             "Priority Pass lounge access", None, None,
             "£24 per person per visit; 1,700+ lounges", UK_PREMIER_URL),
            ("benefit_uk_premier_points", "hsbc_uk_premier", "other",
             "Reward points with airline and hotel partners", None, None,
             "1.5 points per £1 sterling; 2 points per £1 non-sterling",
             UK_PREMIER_URL),
            ("benefit_uk_premier_travel_ins", "hsbc_uk_premier", "travel_insurance",
             "Premier worldwide travel insurance (account benefit)", None, None,
             "Via HSBC Premier Account eligibility", UK_PREMIER_URL),
            ("benefit_uk_we_lounge", "hsbc_uk_premier_world_elite", "airport_lounge",
             "Enhanced travel lounge / fast-track privileges", None, None,
             "Enhanced travel benefits for Premier World Elite", UK_WORLD_ELITE_URL),
            ("benefit_uk_we_points", "hsbc_uk_premier_world_elite", "other",
             "Faster reward points earn", None, None,
             "3 points per £1 sterling; 4 points per £1 non-sterling",
             UK_WORLD_ELITE_URL),
        ]
        for row in benefits:
            connection.execute(
                """INSERT INTO CARD_BENEFIT
                   (benefit_id, card_id, benefit_type, benefit_name, quantity, period,
                    description, source_url, last_verified_at)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (*row, verified_at),
            )

        # Eligibility: age + banking relationship. Group-level copy already states
        # residency / account prerequisites; avoid HK soft-residency messaging on
        # UK/US cards by not inserting residency rules here.
        rules = [
            ("hsbc_us_premier_world:age", "hsbc_us_premier_world", "minimum_age", "gte",
             None, Decimal("18"), None, "hard",
             "Applicants must be at least 18.", US_PREMIER_URL),
            ("hsbc_us_premier_world:premier", "hsbc_us_premier_world",
             "banking_relationship", "eq", "us_premier_checking", None, None, "hard",
             "Requires an existing U.S. HSBC Premier checking account.", US_PREMIER_URL),
            ("hsbc_us_elite:age", "hsbc_us_elite", "minimum_age", "gte",
             None, Decimal("18"), None, "hard",
             "Applicants must be at least 18.", US_ELITE_URL),
            ("hsbc_us_elite:premier", "hsbc_us_elite",
             "banking_relationship", "eq", "us_premier_checking", None, None, "hard",
             "Requires an existing U.S. HSBC Premier checking account.", US_ELITE_URL),
            ("hsbc_uk_student:age", "hsbc_uk_student", "minimum_age", "gte",
             None, Decimal("18"), None, "hard",
             "Applicants must be over 18.", UK_STUDENT_URL),
            ("hsbc_uk_student:account", "hsbc_uk_student",
             "banking_relationship", "eq", "uk_student_account", None, None, "hard",
             "Must hold an HSBC UK Student Bank Account.", UK_STUDENT_URL),
            ("hsbc_uk_premier:age", "hsbc_uk_premier", "minimum_age", "gte",
             None, Decimal("18"), None, "hard",
             "Applicants must be over 18.", UK_PREMIER_URL),
            ("hsbc_uk_premier:premier", "hsbc_uk_premier",
             "banking_relationship", "eq", "uk_premier_account", None, None, "hard",
             "Must hold an HSBC UK Premier Account.", UK_PREMIER_URL),
            ("hsbc_uk_premier_world_elite:age", "hsbc_uk_premier_world_elite",
             "minimum_age", "gte", None, Decimal("18"), None, "hard",
             "Applicants must be over 18.", UK_WORLD_ELITE_URL),
            ("hsbc_uk_premier_world_elite:premier", "hsbc_uk_premier_world_elite",
             "banking_relationship", "eq", "uk_premier_account", None, None, "hard",
             "Must hold an HSBC UK Premier Account (or Private Banking).",
             UK_WORLD_ELITE_URL),
        ]
        for (rule_id, card_id, rule_type, operator, text, number, currency, path,
             description, source) in rules:
            connection.execute(
                """INSERT INTO CARD_ELIGIBILITY_RULE
                   (eligibility_rule_id, card_id, rule_type, operator, value_text,
                    value_numeric, currency, required, description, path_id,
                    outcome_if_met, application_channel, source_url, last_verified_at)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,1,%s,%s,'meets_public_criteria','all',%s,%s)""",
                (rule_id, card_id, rule_type, operator, text, number, currency,
                 description, path, source, verified_at),
            )

        rewards = [
            ("reward_us_premier_gas", "hsbc_us_premier_world", "gas_and_groceries",
             "points", Decimal("3"),
             "3× Points on gas and groceries", US_PREMIER_URL),
            ("reward_us_premier_travel", "hsbc_us_premier_world", "travel",
             "points", Decimal("2"),
             "2× Points on travel", US_PREMIER_URL),
            ("reward_us_premier_general", "hsbc_us_premier_world", "general",
             "points", Decimal("1"),
             "1× Points on other purchases", US_PREMIER_URL),
            ("reward_us_elite_travel", "hsbc_us_elite", "travel",
             "points", Decimal("5"),
             "5× Points on travel", US_ELITE_URL),
            ("reward_us_elite_dining", "hsbc_us_elite", "dining",
             "points", Decimal("2"),
             "2× Points on dining", US_ELITE_URL),
            ("reward_us_elite_general", "hsbc_us_elite", "general",
             "points", Decimal("1"),
             "1× Points on other purchases", US_ELITE_URL),
            ("reward_uk_premier_gbp", "hsbc_uk_premier", "general",
             "points", Decimal("1.5"),
             "1.5 reward points per £1 sterling spend", UK_PREMIER_URL),
            ("reward_uk_we_gbp", "hsbc_uk_premier_world_elite", "general",
             "points", Decimal("3"),
             "3 reward points per £1 sterling spend", UK_WORLD_ELITE_URL),
        ]
        for reward_id, card_id, category, reward_type, points, description, url in rewards:
            connection.execute(
                """INSERT INTO CARD_REWARD_RULE
                   (reward_rule_id, card_id, spend_category, reward_type,
                    points_per_currency_unit, description, source_url, last_verified_at)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
                (reward_id, card_id, category, reward_type, points, description, url,
                 verified_at),
            )

        connection.commit()
        counts = connection.execute(
            """SELECT market, COUNT(*) AS n FROM CREDIT_CARD
               WHERE card_id = ANY(%s) GROUP BY market ORDER BY market""",
            (list(CARD_IDS),),
        ).fetchall()
        by_market = {}
        for row in counts:
            market = row["market"] if isinstance(row, dict) else row[0]
            n = row["n"] if isinstance(row, dict) else row[1]
            by_market[market] = int(n)
        return {
            "cards": list(CARD_IDS),
            "by_market": by_market,
            "verified_at": verified_at.isoformat(),
        }
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


if __name__ == "__main__":
    print(sync())
