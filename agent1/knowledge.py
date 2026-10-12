"""知识只读接口（设计 4.3-6 / 5.2 Knowledge Query / Citation）。

仅返回已发布且当前可用的知识：适用范围、字段级证据、版本与最近成功核验时间。
超过新鲜度 SLA 的数据默认不作为确定事实返回（PRD 7.4），可用 include_stale 查看并标记。
"""
import json
from datetime import datetime, timezone

from . import config, db as db_mod


def _availability(row, now):
    if row["availability"] == "withdrawn":
        return "withdrawn"
    sla_hours = row.get("freshness_sla_hours") or 168
    verified = db_mod.parse_ts(row.get("verified_at"))
    if verified and (now - verified).total_seconds() / 3600 > sla_hours:
        return "stale"
    return "available"


def query(schema_type=None, source_id=None, include_stale=False, now=None):
    now = now or datetime.now(timezone.utc)
    sql = """
      SELECT i.policy_key, i.source_id, i.schema_type, i.applies_to_city, i.version_id,
             i.verified_at, i.availability, v.payload_json, v.risk_level, v.status,
             v.snapshot_hash, v.version_no, s.name AS source_name, s.url AS source_url,
             s.freshness_sla_hours
      FROM knowledge_index i
      JOIN knowledge_versions v ON v.version_id = i.version_id
      JOIN source_registry s ON s.source_id = i.source_id
      WHERE (? IS NULL OR i.schema_type = ?) AND (? IS NULL OR i.source_id = ?)
      ORDER BY i.source_id, i.applies_to_city
    """
    with db_mod.session() as db:
        rows = [dict(r) for r in db.execute(sql, (schema_type, schema_type, source_id, source_id))]
    records = []
    for row in rows:
        availability = _availability(row, now)
        if availability == "withdrawn":
            continue
        if availability == "stale" and not include_stale:
            continue
        payload = json.loads(row["payload_json"])
        verified = db_mod.parse_ts(row.get("verified_at"))
        staleness_hours = round((now - verified).total_seconds() / 3600, 1) if verified else None
        if row["schema_type"] == "visa_policy":
            entry = next((p for p in payload.get("policies", [])
                          if (p.get("applies_to_city") or None) == (row["applies_to_city"] or None)), None)
            policy = entry.get("policy", {}) if entry else {}
            evidence = entry.get("evidence", []) if entry else []
            documents = payload.get("documents", [])
        else:
            policy = {"facts": payload.get("facts", [])}
            evidence = payload.get("facts", [])
            documents = []
        records.append({
            "policy_key": row["policy_key"],
            "source_id": row["source_id"],
            "source_name": row["source_name"],
            "source_url": row["source_url"],
            "schema_type": row["schema_type"],
            "applies_to_city": row["applies_to_city"],
            "version_id": row["version_id"],
            "version_no": row["version_no"],
            "verified_at": row.get("verified_at"),
            "staleness_hours": staleness_hours,
            "sla_hours": row.get("freshness_sla_hours"),
            "availability": availability,
            "risk_level": row["risk_level"],
            "policy": policy,
            "evidence": evidence,
            "documents": documents,
            "snapshot_hash": row["snapshot_hash"],
        })
    return records
