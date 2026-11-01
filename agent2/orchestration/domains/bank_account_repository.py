"""Read-only adapters for HSBC account knowledge."""

from __future__ import annotations

import re
import uuid
from datetime import date, datetime

import anyio

from agent1 import storage

def _query_tokens(text: str) -> set[str]:
    """Cheap lexical tokens for chunk ranking: CJK bigrams, latin words, numbers."""
    tokens: set[str] = set()
    for word in re.findall(r"[A-Za-z]{2,}|\d[\d,.]*", text.casefold()):
        tokens.add(word)
    for cjk in re.findall(r"[\u4e00-\u9fff]+", text):
        if len(cjk) == 1:
            tokens.add(cjk)
        for index in range(len(cjk) - 1):
            tokens.add(cjk[index:index + 2])
    return tokens


def rank_chunks(chunks: list[dict], query: str, limit: int = 4) -> list[dict]:
    """Rank source-document chunks against the question (lexical overlap)."""
    if not chunks:
        return []
    query_tokens = _query_tokens(query)
    scored = []
    for chunk in chunks:
        chunk_tokens = _query_tokens(
            f"{chunk.get('heading') or ''} {chunk.get('content') or ''}")
        overlap = len(query_tokens & chunk_tokens)
        # Coverage matters more than raw count; heading hits weigh double.
        heading_tokens = _query_tokens(chunk.get("heading") or "")
        overlap += 2 * len(query_tokens & heading_tokens)
        scored.append((overlap, chunk.get("seq", 0), chunk))
    scored.sort(key=lambda item: (-item[0], item[1]))
    return [chunk for overlap, _seq, chunk in scored if overlap > 0][:limit]

RDS_ACCOUNT_IDS = {
    "acct_hk_one": "hsbc_hk_one",
    "acct_hk_premier": "hsbc_hk_premier",
    "acct_hk_children_savings": "hsbc_hk_children_savings",
    "acct_hk_teens": "hsbc_hk_teens",
    "acct_hk_premier_elite": "hsbc_hk_premier_elite",
    "acct_hk_hkd_savings": "hsbc_hk_hkd_savings",
    "acct_hk_hkd_current": "hsbc_hk_hkd_current",
    "acct_hk_time_deposit": "hsbc_hk_time_deposit",
    "acct_hk_basic_independence": "hsbc_hk_basic_independence",
    "acct_uk_bank": "hsbc_uk_bank",
    "acct_uk_international_student": "hsbc_uk_international_student",
    "acct_uk_premier": "hsbc_uk_premier",
    "acct_uk_student": "hsbc_uk_student",
    "acct_us_premier": "hsbc_us_premier_checking",
}
RULE_FIELDS = {
    "minimum_age": "applicant_age",
    "maximum_age": "applicant_age",
    "residency_duration": "uk_residency_years",
    "course_status": "qualifying_course_offer",
    "student_status": "applicant_identity",
    "existing_customer": "existing_hsbc_hk_customer",
}


class BankAccountRepositoryError(RuntimeError):
    pass


def _json_value(value):
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return value


def _row(row) -> dict:
    return {key: _json_value(value) for key, value in dict(row).items()}


class RdsBankAccountRepository:
    """Read the shared PostgreSQL account tables through agent1's RDS config."""

    async def read(self, market: str) -> dict:
        return await anyio.to_thread.run_sync(self._load, market)

    async def read_source_chunks(self, account_ids: list[str], query: str,
                                 limit: int = 4) -> list[dict]:
        """Official-page chunks for the given (logical) account ids, ranked by query."""
        return await anyio.to_thread.run_sync(
            self._load_source_chunks, account_ids, query, limit)

    def _load_source_chunks(self, account_ids: list[str], query: str,
                            limit: int) -> list[dict]:
        # RDS_ACCOUNT_IDS maps physical -> logical; chunks are stored with the
        # physical ACCOUNT_PRODUCT.account_id, so invert for the lookup.
        logical_to_physical = {logical: physical for physical, logical in RDS_ACCOUNT_IDS.items()}
        physical_ids = sorted({logical_to_physical.get(value, value) for value in account_ids})
        try:
            with storage.read_session() as db:
                rows = [_row(row) for row in db.execute(
                    """SELECT c.chunk_id, c.doc_id, c.account_id, c.seq, c.heading,
                              c.content, d.title, d.source_url, d.oss_bucket, d.oss_key
                       FROM ACCOUNT_DOC_CHUNK c
                       JOIN ACCOUNT_SOURCE_DOCUMENT d ON d.doc_id = c.doc_id
                       WHERE c.account_id = ANY(%s)
                       ORDER BY c.account_id, c.seq""", (sorted(physical_ids),))]
        except BankAccountRepositoryError:
            raise
        except Exception:
            # Source retrieval is a supplement, never a hard dependency: a
            # missing table or RDS hiccup must not break the answer path.
            return []
        for row in rows:
            row["account_id"] = RDS_ACCOUNT_IDS.get(row["account_id"], row["account_id"])
        return rank_chunks(rows, query, limit)

    def _load(self, market: str) -> dict:
        rds_market = "HK" if market == "CN-HK" else market
        try:
            with storage.read_session() as db:
                raw_products = [_row(row) for row in db.execute(
                    """SELECT * FROM ACCOUNT_PRODUCT
                       WHERE market=%s AND product_status='ACTIVE'
                       ORDER BY account_id""", (rds_market,))]
                ids = [row["account_id"] for row in raw_products]
                raw = {"routes": [], "rules": [], "documents": [], "fees": [],
                       "features": [], "relations": []}
                if ids:
                    queries = {
                        "routes": "SELECT * FROM ACCOUNT_OPENING_ROUTE WHERE account_id = ANY(%s) ORDER BY route_id",
                        "rules": "SELECT * FROM ACCOUNT_ELIGIBILITY_RULE WHERE account_id = ANY(%s) ORDER BY eligibility_rule_id",
                        "documents": "SELECT * FROM ACCOUNT_REQUIRED_DOCUMENT WHERE account_id = ANY(%s) ORDER BY requirement_id",
                        "fees": "SELECT * FROM ACCOUNT_FEE WHERE account_id = ANY(%s) ORDER BY fee_id",
                        "features": "SELECT * FROM ACCOUNT_FEATURE WHERE account_id = ANY(%s) ORDER BY feature_id",
                        "relations": """SELECT r.*, target.product_name AS target_product_name
                            FROM account_product_relation r
                            JOIN account_product target ON target.account_id=r.target_account_id
                            WHERE r.source_account_id = ANY(%s) ORDER BY r.relation_id""",
                    }
                    for key, query in queries.items():
                        raw[key] = [_row(row) for row in db.execute(query, (ids,))]
        except Exception as exc:
            raise BankAccountRepositoryError(str(exc)) from exc

        def account_id(value):
            product_codes = {row["account_id"]: row.get("product_code") for row in raw_products}
            code = product_codes.get(value)
            if code:
                return f"hsbc_{code}"
            return RDS_ACCOUNT_IDS.get(value, value)

        products = [{
            "account_id": account_id(row["account_id"]), "source_account_id": row["account_id"],
            "bank_code": "HSBC", "market": market, "product_name": row["product_name"],
            "account_type": row["account_type"], "customer_segment": row["customer_segment"],
            "summary": row.get("product_summary") or "", "product_status": row["product_status"],
            "official_url": row.get("official_url"), "verified_at": row["last_verified_at"],
        } for row in raw_products]
        routes = [{
            "route_id": row["route_id"], "account_id": account_id(row["account_id"]),
            "route_name": row["route_name"], "channel": row["application_channel"],
            "applicant_location": row.get("applicant_location"),
            "identity_document": row.get("identity_document_type"),
            "before_arrival": bool(row.get("can_apply_before_arrival")),
            "activation_note": row.get("activation_location"),
            "route_note": row.get("additional_notes") or row.get("processing_time_note") or "请按官网渠道申请。",
            "source_url": row.get("source_url"), "verified_at": row["last_verified_at"],
        } for row in raw["routes"]]
        rules = []
        for row in raw["rules"]:
            field = RULE_FIELDS.get(row["rule_type"])
            operator = row["operator"]
            value_text = row.get("value_text")
            if row["eligibility_rule_id"] == "elig_hk_child_guardian":
                field, operator, value_text = "guardian_present", "eq", "true"
            elif row["rule_type"] == "course_status":
                operator, value_text = "eq", "true"
            elif row["rule_type"] == "student_status":
                operator, value_text = "eq", "student"
            elif field is None or operator not in {"eq", "gte", "lte", "in", "present"}:
                field, operator = "bank_confirmation", "note"
            purpose = "route" if row.get("route_id") else "application"
            if "monthly" in row["description"].casefold() and "fee" in row["description"].casefold():
                purpose = "fee_waiver"
            rules.append({
                "rule_id": row["eligibility_rule_id"], "account_id": account_id(row["account_id"]),
                "route_id": row.get("route_id"), "rule_purpose": purpose, "group_code": None,
                "field_code": field, "operator": operator, "value_text": value_text,
                "value_numeric": row.get("value_numeric"), "currency": row.get("currency"),
                "condition_note": None, "description": row["description"],
                "source_url": row.get("source_url"), "verified_at": row["last_verified_at"],
            })
        documents = [{
            "requirement_id": row["requirement_id"], "account_id": account_id(row["account_id"]),
            "route_id": row.get("route_id"), "document_type": row["document_type"],
            "description": row["document_name"],
            "requirement_level": "required" if row["required"] else "may_request",
            "alternative_group": row.get("alternative_group"),
            "condition_note": row.get("condition_note") or row.get("validity_note"),
            "source_url": row.get("source_url"), "verified_at": row["last_verified_at"],
        } for row in raw["documents"]]
        fees = [{
            "fee_id": row["fee_id"], "account_id": account_id(row["account_id"]),
            "fee_type": row["fee_type"], "amount": row.get("amount"), "rate": row.get("rate"),
            "currency": row.get("currency"), "condition_note": row.get("description"),
            "waiver_note": row.get("waiver_condition"), "effective_from": None,
            "source_url": row.get("source_url"), "verified_at": row["last_verified_at"],
        } for row in raw["fees"]]
        relations = [{
            "relation_id": row["relation_id"],
            "source_account_id": account_id(row["source_account_id"]),
            "target_account_id": account_id(row["target_account_id"]),
            "target_product_name": row["target_product_name"],
            "relation_type": row["relation_type"], "message": row["description"],
            "source_url": row["source_url"], "verified_at": row["last_verified_at"],
        } for row in raw["relations"]]
        features = [{
            "feature_id": row["feature_id"], "account_id": account_id(row["account_id"]),
            "feature_type": row["feature_type"], "available": bool(row["available"]),
            "value_text": row.get("value_text"), "description": row["description"],
            "source_url": row.get("source_url"), "verified_at": row["last_verified_at"],
        } for row in raw["features"]]
        return {"read_id": f"rds-hsbc-account-{uuid.uuid4()}", "market": market,
                "products": products, "routes": routes, "rules": rules,
                "documents": documents, "fees": fees, "features": features,
                "relations": relations}
