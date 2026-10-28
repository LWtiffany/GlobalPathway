"""Replace HSBC HK card eligibility rows with sourced, alternative paths."""

from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agent1 import storage


DOCUMENTS_URL = "https://www.hsbc.com.hk/credit-cards/documents-required/"
APPLICANT_NOTES_URL = (
    "https://www.hsbc.com.hk/content/dam/hsbc/hk/sc/docs/credit-cards/"
    "applicants-important-notes-and-declarations.pdf"
)
PRODUCT_URLS = {
    "hsbc_easy": "https://www.hsbc.com.hk/credit-cards/products/visa-platinum/",
    "hsbc_everymile": "https://www.hsbc.com.hk/credit-cards/products/everymile/",
    "hsbc_premier_mastercard": "https://www.hsbc.com.hk/credit-cards/products/premier-mastercard/",
    "hsbc_pulse_dual_currency": "https://www.hsbc.com.hk/credit-cards/products/unionpay-dual-currency-diamond/",
    "hsbc_red": "https://www.hsbc.com.hk/credit-cards/products/red/",
    "hsbc_unionpay_dual_currency": "https://www.hsbc.com.hk/credit-cards/products/unionpay-dual-currency-classic/",
    "hsbc_visa_gold": "https://www.hsbc.com.hk/credit-cards/products/visa-gold/",
    "hsbc_visa_gold_student": "https://www.hsbc.com.hk/credit-cards/products/student-visa-gold/",
    "hsbc_visa_signature": "https://www.hsbc.com.hk/credit-cards/products/visa-signature/",
}
INCOME_THRESHOLDS = {
    "hsbc_easy": 120000,
    "hsbc_everymile": 240000,
    "hsbc_pulse_dual_currency": 120000,
    "hsbc_red": 120000,
    "hsbc_unionpay_dual_currency": 60000,
    "hsbc_visa_gold": 60000,
    "hsbc_visa_signature": 240000,
}


def _row(rule_id, card_id, rule_type, operator, *, text=None, number=None,
         currency=None, path="hard", outcome="meets_public_criteria", channel="all",
         description, source, verified_at):
    return (rule_id, card_id, rule_type, operator, text, number, currency, 1, path,
            outcome, channel, description, source, verified_at)


def sync():
    verified_at = datetime.now(timezone.utc)
    migration = (Path(__file__).resolve().parents[1] / "agent1" / "migrations" /
                 "010_credit_card_eligibility_paths.sql").read_text(encoding="utf-8")
    connection = storage._connect()
    try:
        connection.execute(migration)
        connection.execute(
            "UPDATE CREDIT_CARD SET product_status='DISCONTINUED' "
            "WHERE card_id='hsbc_visa_platinum'"
        )
        card_ids = list(PRODUCT_URLS)
        connection.execute(
            "DELETE FROM CARD_ELIGIBILITY_RULE WHERE card_id = ANY(%s)",
            (card_ids + ["hsbc_visa_platinum"],),
        )
        rows = []
        for card_id, product_url in PRODUCT_URLS.items():
            rows.extend([
                _row(f"{card_id}:age", card_id, "minimum_age", "gte", number=Decimal("18"),
                     description="基本卡申请人须年满18岁。", source=APPLICANT_NOTES_URL,
                     verified_at=verified_at),
                _row(f"{card_id}:residency", card_id, "residency", "eq",
                     text="Hong Kong resident", description="基本卡申请人须为香港特别行政区居民。",
                     source=APPLICANT_NOTES_URL, verified_at=verified_at),
            ])

        for card_id, threshold in INCOME_THRESHOLDS.items():
            rows.append(_row(
                f"{card_id}:income", card_id, "minimum_income", "gte",
                number=Decimal(threshold), currency="HKD", path="income",
                description=f"当前产品页公布最低年收入为HKD{threshold:,}。",
                source=PRODUCT_URLS[card_id], verified_at=verified_at,
            ))
            rows.append(_row(
                f"{card_id}:asset-review", card_id, "asset_proof", "available",
                text="Bank-issued asset proof from latest 3 months", path="asset_review",
                outcome="needs_bank_assessment", channel="document_submission",
                description="没有收入证明的学生等申请人可提交最近3个月的银行资产证明，由HSBC个案审批。",
                source=DOCUMENTS_URL, verified_at=verified_at,
            ))

        rows.append(_row(
            "hsbc_visa_signature:explicit-assets", "hsbc_visa_signature", "minimum_assets",
            "gte", number=Decimal("400000"), currency="HKD", path="explicit_assets",
            channel="phone", description="电话申请声明公布资产HKD400,000或以上的替代路径。",
            source=APPLICANT_NOTES_URL, verified_at=verified_at,
        ))
        rows.extend([
            _row("hsbc_visa_gold_student:student", "hsbc_visa_gold_student",
                 "student_status", "required", text="Full-time tertiary student",
                 path="student", description="申请人须为全日制大专或大学生。",
                 source=PRODUCT_URLS["hsbc_visa_gold_student"], verified_at=verified_at),
            _row("hsbc_visa_gold_student:institution", "hsbc_visa_gold_student",
                 "institution_status", "required", text="HSBC designated Hong Kong institution",
                 path="student", description="申请人须就读HSBC列出的香港指定院校。",
                 source=PRODUCT_URLS["hsbc_visa_gold_student"], verified_at=verified_at),
            _row("hsbc_premier_mastercard:relationship", "hsbc_premier_mastercard",
                 "banking_relationship", "required", text="HSBC Premier",
                 path="premier", description="仅供HSBC Premier客户申请。",
                 source=PRODUCT_URLS["hsbc_premier_mastercard"], verified_at=verified_at),
            _row("hsbc_premier_mastercard:balance", "hsbc_premier_mastercard",
                 "relationship_balance", "gte", number=Decimal("100000"), currency="HKD",
                 path="premier", description="申请后两个月内全面理财总值须至少HKD100,000（不含借贷）。",
                 source=APPLICANT_NOTES_URL, verified_at=verified_at),
            _row("hsbc_premier_mastercard:qualify-balance", "hsbc_premier_mastercard",
                 "relationship_balance", "gte", number=Decimal("1000000"), currency="HKD",
                 path="qualify_premier", outcome="needs_bank_assessment",
                 description="可先以HKD1,000,000总关系结余申请或升级HSBC Premier，再申请信用卡。",
                 source=PRODUCT_URLS["hsbc_premier_mastercard"], verified_at=verified_at),
        ])

        with connection.cursor() as cursor:
            cursor.executemany(
                """INSERT INTO CARD_ELIGIBILITY_RULE (
                    eligibility_rule_id, card_id, rule_type, operator, value_text,
                    value_numeric, currency, required, path_id, outcome_if_met,
                    application_channel, description, source_url, last_verified_at
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                rows,
            )
        connection.commit()
        return {"cards": len(card_ids), "rules": len(rows),
                "verified_at": verified_at.isoformat()}
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


if __name__ == "__main__":
    print(sync())
