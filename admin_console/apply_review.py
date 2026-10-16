"""Approve/reject logic ported from Agent1 (formal_pipeline / fx_pipeline / tuition_pipeline).

SQLite dialect was translated to PostgreSQL: ``?`` -> ``%s``, ``INSERT OR IGNORE`` /
``INSERT OR REPLACE`` -> ``ON CONFLICT``, and the attached ``admin`` schema became the
PostgreSQL ``admin`` schema.
"""
import json
import uuid
from datetime import datetime, timezone

VISA_FIELD_COLUMNS = (
    "effective_from", "effective_to", "cas_required", "i20_required",
    "funds_tuition_unpaid", "funds_tuition_currency", "funds_living_monthly",
    "funds_living_currency", "funds_living_months", "funds_holding_days",
    "funds_doc_max_age_days", "funds_owner_allowed", "english_level", "tb_test_required",
    "health_surcharge", "apply_window_days", "processing_days", "visa_fee",
    "applies_to_city", "remark",
)

FX_FIELDS = (
    "effective_from", "effective_to", "annual_quota_amount", "annual_quota_currency",
    "quota_reset", "over_quota_allowed", "tuition_direct_to_school",
    "remitter_relation", "can_use_family_quota",
)


def _now():
    return datetime.now(timezone.utc).isoformat()


def _json(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False)


def _row(connection, sql, parameters=()):
    return connection.execute(sql, parameters).fetchone()


def _document_link(document):
    code = document["code"].strip()
    # doc_id 必须含 name_zh：other_document 是兜底码，多种材料共用，只按 code 会塌缩。
    name = (document.get("name_zh") or code).strip()
    doc_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"document:{code.casefold()}:{name.casefold()}"))
    required = document.get("required")
    condition = document.get("condition") if required is not None else None
    return {"doc_id": doc_id, "code": code, "name_zh": name,
            "required": None if required is None else (1 if required else 0),
            "condition": _json(condition) if condition else None}


def _apply_documents(connection, policy_id, documents, domain):
    for document in documents or []:
        if not document["code"].strip():
            continue
        link = _document_link(document)
        connection.execute(
            "INSERT INTO DOCUMENT (doc_id, code, name_zh) VALUES (%s,%s,%s) "
            "ON CONFLICT (doc_id) DO NOTHING",
            (link["doc_id"], link["code"], link["name_zh"]))
        connection.execute(
            'INSERT INTO POLICY_DOCUMENT (policy_id, policy_domain, doc_id, required, "condition") '
            "VALUES (%s,%s,%s,%s,%s) ON CONFLICT (policy_id, policy_domain, doc_id) DO UPDATE "
            'SET required=EXCLUDED.required, "condition"=EXCLUDED."condition"',
            (policy_id, domain, link["doc_id"], link["required"], link["condition"]))


def _remove_documents(connection, policy_id, documents, domain):
    for document in documents or []:
        connection.execute(
            "DELETE FROM POLICY_DOCUMENT WHERE policy_id=%s AND policy_domain=%s AND doc_id=%s",
            (policy_id, domain, document["doc_id"]))
        evidence_table = "POLICY_EVIDENCE" if domain == "visa_policy" else "FX_EVIDENCE"
        connection.execute(f"DELETE FROM {evidence_table} WHERE policy_id=%s AND target=%s",
                           (policy_id, f"document:{document['doc_id']}"))


def _replace_evidence(connection, table, key_col, key_val, target, items, prefix):
    """items: (kind, source_id, snapshot_id, quote)；不可逐字追溯的引用不入库。"""
    items = list(dict.fromkeys(i for i in items if all(i)))
    if not items:
        return
    connection.execute(f"DELETE FROM {table} WHERE {key_col}=%s AND target=%s", (key_val, target))
    now = _now()
    for kind, source_id, snapshot_id, quote in items:
        evidence_id = str(uuid.uuid5(uuid.NAMESPACE_URL, "|".join(
            (prefix, key_val, target, kind, source_id, snapshot_id, quote))))
        connection.execute(
            f"INSERT INTO {table} (evidence_id, {key_col}, target, kind, source_id, snapshot_id, quote, recorded_at) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (evidence_id) DO NOTHING",
            (evidence_id, key_val, target, kind, source_id, snapshot_id, quote, now))


def _record_visa_evidence(connection, policy_id, values, evidence, documents):
    row = _row(connection, "SELECT * FROM VISA_POLICY WHERE policy_id=%s", (policy_id,))
    if row is None:
        return
    by_field = {}
    for ev in evidence:
        by_field.setdefault(ev["field"], []).append(
            ("value", ev.get("source_id"), ev.get("snapshot_id"), ev["evidence_quote"]))
    for column, value in values.items():
        if column != "remark" and column in by_field and row.get(column) is not None \
                and row[column] == value:
            _replace_evidence(connection, "POLICY_EVIDENCE", "policy_id", policy_id,
                              f"field:{column}", by_field[column], "evidence")
    for document in documents or []:
        if not document["code"].strip():
            continue
        link = _document_link(document)
        current = _row(connection, 'SELECT required, "condition" FROM POLICY_DOCUMENT '
                       "WHERE policy_id=%s AND policy_domain='visa_policy' AND doc_id=%s",
                       (policy_id, link["doc_id"]))
        if current is None or (current["required"], current["condition"]) != \
                (link["required"], link["condition"]):
            continue
        items = [("value", s["source_id"], s["snapshot_id"], s["evidence_quote"])
                 for s in document.get("support") or []]
        _replace_evidence(connection, "POLICY_EVIDENCE", "policy_id", policy_id,
                          f"document:{link['doc_id']}", items, "evidence")


def _record_fx_evidence(connection, policy_id, evidence, documents):
    row = _row(connection, "SELECT * FROM FX_POLICY WHERE policy_id=%s", (policy_id,))
    if row is None:
        return
    for field, facts in evidence.items():
        values = {fact["value"] for fact in facts}
        if row.get(field) is None or row[field] not in values:
            continue
        _replace_evidence(connection, "FX_EVIDENCE", "policy_id", policy_id, f"field:{field}", [
            ("value", f.get("source_id"), f.get("snapshot_id"), f["evidence_quote"])
            for f in facts if f["value"] == row[field]], "fx_evidence")
    for document in documents or []:
        if not document["code"].strip():
            continue
        link = _document_link(document)
        current = _row(connection, 'SELECT required, "condition" FROM POLICY_DOCUMENT '
                       "WHERE policy_id=%s AND policy_domain='fx_policy' AND doc_id=%s",
                       (policy_id, link["doc_id"]))
        if current is None or (current["required"], current["condition"]) != \
                (link["required"], link["condition"]):
            continue
        items = [("value", s["source_id"], s["snapshot_id"], s["evidence_quote"])
                 for s in document.get("support") or []]
        _replace_evidence(connection, "FX_EVIDENCE", "policy_id", policy_id,
                          f"document:{link['doc_id']}", items, "fx_evidence")


def _replace_tuition_evidence(connection, tuition_id, rows):
    items = []
    for row in rows:
        if not (row.get("source_id") and row.get("snapshot_id")):
            continue
        items.append(("amount", row["source_id"], row["snapshot_id"], row["evidence_quote"]))
        items += [("context", row["source_id"], row["snapshot_id"], q)
                  for q in row.get("context_quotes") or []]
    items = list(dict.fromkeys(items))
    if not items:
        return
    connection.execute("DELETE FROM TUITION_EVIDENCE WHERE tuition_id=%s", (tuition_id,))
    now = _now()
    for kind, source_id, snapshot_id, quote in items:
        evidence_id = str(uuid.uuid5(uuid.NAMESPACE_URL, "|".join(
            ("tuition_evidence", tuition_id, kind, source_id, snapshot_id, quote))))
        connection.execute(
            "INSERT INTO TUITION_EVIDENCE (evidence_id, tuition_id, kind, source_id, snapshot_id, quote, recorded_at) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (evidence_id) DO NOTHING",
            (evidence_id, tuition_id, kind, source_id, snapshot_id, quote, now))


def _apply_visa_review(connection, row):
    if row["domain_table"] != "VISA_POLICY":
        raise ValueError(f"暂不支持审核域表：{row['domain_table']}")
    payload = json.loads(row["proposed_value"])
    policy = payload["policy"]
    policy_id = row["record_key"]
    field_columns = [c for c in policy if c in VISA_FIELD_COLUMNS]
    if row["action"] == "CREATE":
        existing = _row(connection, "SELECT policy_id FROM VISA_POLICY WHERE policy_id=%s", (policy_id,))
        if existing:
            raise ValueError(f"目标行已存在：{policy_id}；请用 UPDATE 类审核处理")
        connection.execute(
            f"INSERT INTO VISA_POLICY (policy_id, country, policy_type, title, {', '.join(field_columns)}) "
            f"VALUES (%s,%s,%s,%s,{', '.join('%s' for _ in field_columns)})",
            [policy_id, policy.get("country", "unknown"), policy.get("policy_type", "student"),
             policy.get("title", policy_id)] + [policy[c] for c in field_columns])
    elif field_columns:
        connection.execute(f"UPDATE VISA_POLICY SET {', '.join(f'{c}=%s' for c in field_columns)} "
                           "WHERE policy_id=%s", [policy[c] for c in field_columns] + [policy_id])
    _apply_documents(connection, policy_id, payload.get("documents"), "visa_policy")
    _remove_documents(connection, policy_id, payload.get("removed_documents"), "visa_policy")
    _record_visa_evidence(connection, policy_id, {c: policy[c] for c in field_columns},
                          json.loads(row["evidence_json"] or "[]"), payload.get("documents"))
    return {"review_id": row["review_id"], "action": row["action"], "policy_id": policy_id,
            "applied_fields": field_columns}


def _apply_fx_review(connection, row):
    payload = json.loads(row["proposed_value"])
    policy = payload["policy"]
    id_ = row["record_key"]
    columns = [c for c in policy if c in FX_FIELDS]
    if row["action"] == "CREATE":
        if _row(connection, "SELECT policy_id FROM FX_POLICY WHERE policy_id=%s", (id_,)):
            raise ValueError(f"目标行已存在：{id_}；请用 UPDATE 类审核处理")
        insert = ["policy_id", "country", "policy_type", "title", "purpose_code", *columns]
        connection.execute(f"INSERT INTO FX_POLICY ({', '.join(insert)}) "
                           f"VALUES ({', '.join('%s' for _ in insert)})",
                           [id_, policy.get("country", "unknown"), policy.get("policy_type", "study_abroad"),
                            policy.get("title", id_), policy.get("purpose_code")]
                           + [policy[c] for c in columns])
    elif columns:
        connection.execute(f"UPDATE FX_POLICY SET {', '.join(f'{c}=%s' for c in columns)} "
                           "WHERE policy_id=%s", [policy[c] for c in columns] + [id_])
    _apply_documents(connection, id_, payload.get("documents"), "fx_policy")
    _remove_documents(connection, id_, payload.get("removed_documents"), "fx_policy")
    evidence = {}
    for fact in json.loads(row["evidence_json"] or "[]"):
        if fact.get("field") in columns:
            evidence.setdefault(fact["field"], []).append(fact)
    _record_fx_evidence(connection, id_, evidence, payload.get("documents"))
    return {"review_id": row["review_id"], "action": row["action"], "policy_id": id_,
            "applied_fields": columns}


def _apply_tuition_review(connection, row):
    payload = json.loads(row["proposed_value"])
    tuition = payload["tuition"]
    tid = row["record_key"]
    if _row(connection, "SELECT program_id FROM PROGRAM WHERE program_id=%s",
            (tuition["program_id"],)) is None:
        raise ValueError(f"项目不存在：{tuition['program_id']}；请先重新扫描该学校")
    existing = _row(connection, "SELECT tuition_id FROM TUITION WHERE tuition_id=%s", (tid,))
    if row["action"] == "CREATE":
        if existing:
            raise ValueError(f"目标行已存在：{tid}；请用 UPDATE 类审核处理")
        columns = list(tuition)
        connection.execute(f"INSERT INTO TUITION ({', '.join(columns)}) "
                           f"VALUES ({', '.join('%s' for _ in columns)})",
                           [tuition[c] for c in columns])
    else:
        if not existing:
            raise ValueError(f"目标行不存在：{tid}")
        connection.execute("UPDATE TUITION SET amount=%s, currency=%s, remark=%s WHERE tuition_id=%s",
                           (tuition["amount"], tuition["currency"], tuition.get("remark"), tid))
    support = [r for r in json.loads(row["evidence_json"] or "[]")
               if float(r.get("amount") or 0) == float(tuition["amount"])
               and r.get("currency") == tuition["currency"]]
    _replace_tuition_evidence(connection, tid, support)
    return {"review_id": row["review_id"], "action": row["action"], "tuition_id": tid,
            "applied_fields": ["amount", "currency"]}


def apply_review(connection, review_id, *, decided_by, note=None):
    """Approve one admin.review_queue row: apply its payload to the domain table, then drop it."""
    row = _row(connection, "SELECT * FROM admin.review_queue WHERE review_id=%s", (review_id,))
    if not row:
        raise ValueError(f"没有待处理的审核项：{review_id}")
    domain = row["domain_table"]
    if domain == "FX_POLICY":
        result = _apply_fx_review(connection, row)
    elif domain == "TUITION":
        result = _apply_tuition_review(connection, row)
    else:
        result = _apply_visa_review(connection, row)
    connection.execute("DELETE FROM admin.review_queue WHERE review_id=%s", (review_id,))
    return {**result, "status": "APPROVED", "decided_by": decided_by, "note": note}


def reject_review(connection, review_id, *, decided_by, note):
    """Discard one review: the domain table is left untouched and the row is dropped."""
    cur = connection.execute("DELETE FROM admin.review_queue WHERE review_id=%s", (review_id,))
    if cur.rowcount == 0:
        raise ValueError(f"没有待处理的审核项：{review_id}")
    return {"review_id": review_id, "status": "REJECTED", "decided_by": decided_by, "note": note}
