"""Incremental capture straight into the clean domain schema (db-er-diagram.mmd).

A destination (sources.json `region`) is described by several official pages
(overview, money, English, documents, fees ...). Each page is captured on its own:
conditional probe -> Crawl4AI -> per-section LLM extraction (cache in
section_cache.json) -> per-page merge + gate against that page's snapshot. The
gate-clean facts of every page of the region are then merged into ONE set of
VISA_POLICY rows keyed by (country, scope), so a fact found on any page lands in
the same row.

Pipeline mechanics (check state, section cache, snapshots) live in JSON files;
the database holds the domain tables, schema_migrations, REVIEW_QUEUE and
GATE_ISSUE (the persisted gate problems behind each review reason).

Write policy (field level):
  - a field with a gate error or a cross-page conflict never auto-applies; it is
    queued in REVIEW_QUEUE with its evidence
  - scope not in VISA_POLICY yet -> INSERT the clean fields (+ DOCUMENT / POLICY_DOCUMENT)
  - scope already in VISA_POLICY:
      clean value for an empty column  -> filled directly (same trust as INSERT)
      different value for a set column -> REVIEW_QUEUE (action=UPDATE);
                                          a human applies it via `review approve`
      remark is free text: filled when empty, never queued for rewording
  - documents not linked to the row yet are linked; a changed required/condition
    is queued for review
"""
import asyncio
import json
import re
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

from . import config, crawl, formal_extract, gate, incremental, storage
from .extract import ExtractionError
from .models import VISA_FIELDS, VisaPatch, VisaPolicyData, VisaPolicyEntry

VISA_FIELD_COLUMNS = (
    "effective_from", "effective_to", "cas_required", "i20_required",
    "funds_tuition_unpaid", "funds_tuition_currency", "funds_living_monthly",
    "funds_living_currency", "funds_living_months", "funds_holding_days",
    "funds_doc_max_age_days", "funds_owner_allowed", "english_level", "tb_test_required",
    "health_surcharge", "apply_window_days", "processing_days", "visa_fee",
    "applies_to_city", "remark",
)
# funds_tuition_required / conditions have no column in the ER-diagram schema.
CONDITIONS_MARKER = "[conditions] "
UNCHANGED = ("NOT_MODIFIED", "BODY_UNCHANGED", "NOT_DUE", "CACHED")


def _now():
    return datetime.now(timezone.utc).isoformat()


def _id(prefix):
    return f"{prefix}-{uuid.uuid4().hex}"


def _json(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False)


def _row(connection, sql, parameters=()):
    result = connection.execute(sql, parameters).fetchone()
    return dict(result) if result else None


def _snapshot_file(paths, relative):
    path = paths.data / relative
    if not path.resolve().is_relative_to(paths.snapshots.resolve()):
        raise ValueError("快照路径越过环境目录")
    return path


# ---------- sources.json registry ----------

def _validate_source_id(source_id):
    if not re.fullmatch(r"[a-z][a-z0-9_-]{0,79}", source_id):
        raise ValueError(f"来源 ID 不安全：{source_id}")
    return source_id


def load_sources(path=None):
    """sources.json 是唯一的来源登记（数据库不再保存来源表）。"""
    path = Path(path or config.SOURCES_PATH)
    entries = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(entries, list):
        raise ValueError("来源配置必须为 JSON 数组")
    sources = {}
    for entry in entries:
        _validate_source_id(entry["id"])
        parsed = urlparse(entry["url"])
        if parsed.scheme != "https" or parsed.hostname != entry["allowed_host"]:
            raise ValueError(f"来源 {entry['id']} URL 与登记主机不匹配")
        sources[entry["id"]] = entry
    return sources


def register_sources(path=None, paths=None):
    """校验 sources.json（兼容旧 CLI 名称；不再写库）。返回来源数量。"""
    sources = load_sources(path)
    for source in sources.values():
        if source.get("schema_type") not in ("visa_policy", "fx_policy", "tuition", "generic_facts"):
            raise ValueError(f"来源 {source['id']} 的 schema_type 未知")
        if source["schema_type"] == "tuition":
            from . import tuition_pipeline
            tuition_pipeline.validate_registry(source)
    return len(sources)


def _source(source_id):
    sources = load_sources()
    if source_id not in sources:
        raise KeyError(f"sources.json 中没有来源：{source_id}")
    return sources[source_id]


def visa_regions(sources=None):
    sources = sources if sources is not None else load_sources()
    return sorted({s.get("region") or "unknown" for s in sources.values()
                   if s.get("schema_type") == "visa_policy" and s.get("enabled")})


def _region_sources(region, sources=None):
    sources = sources if sources is not None else load_sources()
    return [s for s in sources.values()
            if s.get("schema_type") == "visa_policy" and s.get("enabled")
            and (s.get("region") or "unknown") == region]


# ---------- JSON pipeline state ----------

def _state_path(paths):
    return paths.data / "check_state.json"


def _cache_path(paths):
    return paths.data / "section_cache.json"


def _load_json(path, default):
    if not path.is_file():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def _save_json(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")


def _load_state(paths, source_id):
    return _load_json(_state_path(paths), {}).get(source_id, {})


def _save_state(paths, source_id, state):
    data = _load_json(_state_path(paths), {})
    data[source_id] = state
    _save_json(_state_path(paths), data)


def _load_cache(paths):
    return _load_json(_cache_path(paths), {})


def _save_cache_entry(paths, section_hash, profile, patch_json):
    data = _load_cache(paths)
    data[f"{section_hash}:{profile}"] = patch_json
    _save_json(_cache_path(paths), data)


def _next_due(source, now):
    return (now + timedelta(minutes=int(source.get("check_interval_minutes", 1440)))).isoformat()


def storage_timestamp(value):
    if not value:
        return None
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


# ---------- domain writes ----------

def _scope_of(proposed):
    return (proposed.get("applies_to_city") or "all").strip().casefold().replace(" ", "_")


def _policy_id(country, scope):
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"visa_policy:{country}:student:{scope}"))


def _row_values(proposed, *, keep_null=False):
    """Extract model dict -> {column: value} for VISA_POLICY (ER-diagram columns only)."""
    row = {}
    for column in VISA_FIELD_COLUMNS:
        if column in proposed and (proposed[column] is not None or keep_null):
            row[column] = proposed[column]
    conditions = proposed.get("conditions")
    if conditions:
        suffix = CONDITIONS_MARKER + json.dumps(conditions, ensure_ascii=False)
        row["remark"] = f"{row.get('remark') or ''}\n{suffix}".strip() if row.get("remark") else suffix
    return row


def _document_link(document):
    code = document["code"].strip()
    # doc_id 必须含 name_zh：other_document 是兜底码，多种材料共用，
    # 只按 code 推导会把它们全部塌缩成同一行（后写覆盖先写）。
    name = (document.get("name_zh") or code).strip()
    doc_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"document:{code.casefold()}:{name.casefold()}"))
    required = document.get("required")
    condition = document.get("condition") if required is not None else None
    return {"doc_id": doc_id, "code": code, "name_zh": name,
            "required": None if required is None else (1 if required else 0),
            "condition": _json(condition) if condition else None}


def _apply_documents(connection, policy_id, documents):
    """Upsert DOCUMENT + POLICY_DOCUMENT for one visa policy scope."""
    for document in documents or []:
        if not document["code"].strip():
            continue
        link = _document_link(document)
        connection.execute("INSERT INTO DOCUMENT (doc_id, code, name_zh) VALUES (%s,%s,%s) "
                           "ON CONFLICT (doc_id) DO NOTHING",
                           (link["doc_id"], link["code"], link["name_zh"]))
        connection.execute('INSERT INTO POLICY_DOCUMENT '
                           '(policy_id, policy_domain, doc_id, required, "condition") '
                           'VALUES (%s,%s,%s,%s,%s) ON CONFLICT (policy_id, policy_domain, doc_id) '
                           'DO UPDATE SET required=EXCLUDED.required, "condition"=EXCLUDED."condition"',
                           (policy_id, "visa_policy", link["doc_id"], link["required"],
                            link["condition"]))


def _sync_documents(connection, policy_id, documents):
    """Link new documents directly; return the ones whose existing link differs."""
    new, changed = [], []
    for document in documents or []:
        if not document["code"].strip():
            continue
        link = _document_link(document)
        existing = _row(connection, """SELECT required, "condition" FROM POLICY_DOCUMENT
            WHERE policy_id=%s AND policy_domain='visa_policy' AND doc_id=%s""",
            (policy_id, link["doc_id"]))
        if existing is None:
            new.append(document)
        elif (existing["required"], existing["condition"]) != (link["required"], link["condition"]):
            changed.append(document)
    _apply_documents(connection, policy_id, new)
    return new, changed


def _vanished_documents(connection, policy_id, named, pages):
    """Linked documents that no section of the region's pages names any more.

    still_quoted: an earlier value quote is still verbatim on its page, so the extraction
    may simply have missed it (the reviewer is told to check before approving removal).
    """
    rows = connection.execute("""SELECT d.doc_id, d.code, d.name_zh FROM POLICY_DOCUMENT pd
        JOIN DOCUMENT d USING (doc_id) WHERE pd.policy_id=%s AND pd.policy_domain='visa_policy'
        ORDER BY d.code, d.name_zh""", (policy_id,))
    vanished = []
    for row in rows:
        if row["doc_id"] in named:
            continue
        quotes = connection.execute("""SELECT source_id, quote FROM POLICY_EVIDENCE
            WHERE policy_id=%s AND target=%s AND kind='value'""",
            (policy_id, f"document:{row['doc_id']}")).fetchall()
        vanished.append({**dict(row), "still_quoted": any(
            gate.quote_in_markdown(q["quote"], pages.get(q["source_id"], "")) for q in quotes)})
    return vanished


def _remove_documents(connection, policy_id, documents):
    for document in documents or []:
        connection.execute("""DELETE FROM POLICY_DOCUMENT
            WHERE policy_id=%s AND policy_domain='visa_policy' AND doc_id=%s""",
                           (policy_id, document["doc_id"]))
        connection.execute("DELETE FROM POLICY_EVIDENCE WHERE policy_id=%s AND target=%s",
                           (policy_id, f"document:{document['doc_id']}"))


def _replace_evidence(connection, policy_id, target, items):
    """items: (kind, source_id, snapshot_id, quote)；没有可追溯来源的引用不入库。"""
    items = list(dict.fromkeys(i for i in items if all(i)))
    if not items:
        return
    connection.execute("DELETE FROM POLICY_EVIDENCE WHERE policy_id=%s AND target=%s",
                       (policy_id, target))
    now = _now()
    for kind, source_id, snapshot_id, quote in items:
        evidence_id = str(uuid.uuid5(uuid.NAMESPACE_URL, "|".join(
            ("evidence", policy_id, target, kind, source_id, snapshot_id, quote))))
        connection.execute("""INSERT INTO POLICY_EVIDENCE
            (evidence_id, policy_id, target, kind, source_id, snapshot_id, quote, recorded_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (evidence_id) DO NOTHING""",
            (evidence_id, policy_id, target, kind, source_id, snapshot_id, quote, now))


def _record_evidence(connection, policy_id, values, evidence, documents):
    """为行里当前保存的取值（与本次支撑的取值相同）刷新逐字证据。"""
    row = _row(connection, "SELECT * FROM VISA_POLICY WHERE policy_id=%s", (policy_id,))
    if row is None:
        return
    by_field = {}
    for ev in evidence:
        ev = ev if isinstance(ev, dict) else ev.model_dump()
        by_field.setdefault(ev["field"], []).append(
            ("value", ev.get("source_id"), ev.get("snapshot_id"), ev["evidence_quote"]))
    for column, value in values.items():
        if column != "remark" and column in by_field and row.get(column) is not None \
                and row[column] == value:
            _replace_evidence(connection, policy_id, f"field:{column}", by_field[column])
    for document in documents or []:
        document = document if isinstance(document, dict) else document.model_dump()
        if not document["code"].strip():
            continue
        link = _document_link(document)
        current = _row(connection, """SELECT required, "condition" FROM POLICY_DOCUMENT
            WHERE policy_id=%s AND policy_domain='visa_policy' AND doc_id=%s""",
            (policy_id, link["doc_id"]))
        if current is None or (current["required"], current["condition"]) != (link["required"], link["condition"]):
            continue
        items = []
        for support in document.get("support") or []:
            items.append(("value", support["source_id"], support["snapshot_id"], support["evidence_quote"]))
            for part in (support.get("condition_quote") or "").split(gate.CONDITION_QUOTE_SEPARATOR):
                if part.strip() and link["condition"]:
                    items.append(("condition", support["source_id"], support["snapshot_id"], part))
        _replace_evidence(connection, policy_id, f"document:{link['doc_id']}", items)


def _enqueue_review(connection, *, table, key, action, payload, evidence, reason):
    # Dedup: one review per (table, key, action) via the admin.review_queue UNIQUE
    # constraint; repeat scans reuse it but refresh the reason/evidence.
    existing = _row(connection, """SELECT review_id FROM admin.review_queue
        WHERE domain_table=%s AND record_key=%s AND action=%s""",
        (table, key, action))
    if existing:
        connection.execute("""UPDATE admin.review_queue
            SET proposed_value=%s, evidence_json=%s, reason=%s WHERE review_id=%s""",
            (_json(payload), _json(evidence), reason, existing["review_id"]))
        return existing["review_id"]
    review_id = _id("review")
    connection.execute("""INSERT INTO admin.review_queue
        (review_id, domain_table, record_key, action, field_path, proposed_value,
         evidence_json, reason, created_at)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
        (review_id, table, key, action, None, _json(payload),
         _json(evidence), reason, _now()))
    return review_id


def _gate_issue_key(problem):
    """Stable identity of a gate problem, used to dedup OPEN rows across runs."""
    return "|".join(str(problem.get(field) or "")
                    for field in ("check", "severity", "scope", "field", "reason"))


def _persist_gate_issues(connection, source, snapshot_id, report):
    """Record this run's gate problems in GATE_ISSUE and auto-resolve stale ones.

    - identical OPEN problems are reused (last_seen_at / snapshot refreshed)
    - OPEN problems no longer reported for this source are marked RESOLVED
    """
    source_id = source["id"]
    now = _now()
    seen_keys = []
    written = []
    for problem in report.problems:
        key = _gate_issue_key(problem)
        seen_keys.append(key)
        existing = _row(connection, """SELECT issue_id FROM GATE_ISSUE
            WHERE source_id=%s AND issue_key=%s AND status='OPEN'""", (source_id, key))
        if existing:
            connection.execute("""UPDATE GATE_ISSUE SET last_seen_at=%s, snapshot_id=%s
                WHERE issue_id=%s""", (now, snapshot_id, existing["issue_id"]))
            if existing["issue_id"] not in written:
                written.append(existing["issue_id"])
            continue
        issue_id = _id("issue")
        connection.execute("""INSERT INTO GATE_ISSUE
            (issue_id, source_id, snapshot_id, scope, check_name, severity, field,
             value, quote, reason, suggested_action, issue_key, status,
             first_seen_at, last_seen_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s, 'OPEN', %s, %s)""",
            (issue_id, source_id, snapshot_id, problem.get("scope"), problem["check"],
             problem["severity"], problem.get("field"), problem.get("value"),
             problem.get("quote"), problem["reason"], problem.get("suggested_action"),
             key, now, now))
        written.append(issue_id)
    if seen_keys:
        placeholders = ",".join("%s" for _ in seen_keys)
        connection.execute(f"""UPDATE GATE_ISSUE SET status='RESOLVED', resolved_at=%s
            WHERE source_id=%s AND status='OPEN' AND issue_key NOT IN ({placeholders})""",
            [now, source_id, *seen_keys])
    else:
        connection.execute("""UPDATE GATE_ISSUE SET status='RESOLVED', resolved_at=%s
            WHERE source_id=%s AND status='OPEN'""", (now, source_id))
    return written


# ---------- gate-clean / blocked split ----------

def split_blocked(patch, report, source_id=None):
    """Remove fields/documents with gate errors from a page patch.

    Returns (clean_patch, blocked) where blocked is a list of
    {"scope", "fields", "evidence", "reasons", "source_id"} for human review.
    A problem blocks a field when it names that field and the entry's scope label
    (or no scope, i.e. page-level). An error on applies_to_city, or a scope-level
    error without a field, blocks the whole entry.
    """
    errors = [p for p in report.problems if p["severity"] == "error"]
    clean_entries, blocked = [], []
    for index, entry in enumerate(patch.policies, start=1):
        label = entry.policy.applies_to_city or f"条目{index}"
        mine = [p for p in errors if p.get("scope") in (label, None)]
        fields = entry.policy.model_dump(exclude_none=True)
        bad = {p["field"] for p in mine if p.get("field") in VISA_FIELDS}
        whole = "applies_to_city" in bad or any(
            p.get("scope") == label and not p.get("field") for p in errors)
        dropped = {f: v for f, v in fields.items()
                   if (whole or f in bad) and f != "applies_to_city"}
        kept = {} if whole else {f: v for f, v in fields.items() if f not in bad}
        if dropped:
            blocked.append({
                "scope": entry.policy.applies_to_city,
                "fields": dropped,
                "evidence": [ev.model_dump() for ev in entry.evidence if ev.field in dropped],
                "reasons": [p["reason"] for p in mine
                            if whole or p.get("field") in dropped],
                "source_id": source_id,
            })
        if set(kept) - {"applies_to_city"}:
            clean_entries.append(VisaPolicyEntry(
                policy=VisaPolicyData.model_validate(kept),
                evidence=[ev for ev in entry.evidence if ev.field in kept]))
    # 材料引用不成立 -> 丢弃；只是条件引用不成立 -> 材料保留，但适用条件记为未知
    drop, unknown = set(), set()
    for problem in errors:
        field = problem.get("field") or ""
        if field.startswith("document:"):
            target = problem.get("document") or field.removeprefix("document:")
            (unknown if problem.get("part") == "condition" else drop).add(target)
    documents = []
    for document in patch.documents:
        keys = {gate.document_key(document), document.code}
        if keys & drop:
            continue
        if keys & unknown:
            document = document.model_copy(update={
                "required": None if document.required else document.required,
                "condition": None, "condition_quote": None})
        documents.append(document)
    return VisaPatch(policies=clean_entries, documents=documents), blocked


def _blocked_for_scope(blocked, scope_key, region):
    """Blocked items of this scope plus universal ones (they apply to every scope)."""
    return [item for item in blocked
            if formal_extract._scope_key(item["scope"], region) in (scope_key, "*")]


def _reject_stale_reviews(connection, policy_id):
    """Close pending VISA proposals once the latest scan needs no review.

    Mirrors fx_pipeline._reject_stale_reviews / tuition_pipeline._reject_stale_reviews:
    a value (or a document requirement) can change and queue an UPDATE review, then
    return to the stored state before a reviewer acts.  Leaving that proposal OPEN
    would let an obsolete value be applied after the later scan already verified the
    current state, and would keep blocking publish.  admin.review_queue has no status
    column, so stale proposals are deleted instead of marked REJECTED.
    """
    connection.execute("""DELETE FROM admin.review_queue
        WHERE domain_table='VISA_POLICY' AND record_key=%s""",
                       (policy_id,))


def _apply_region(connection, region, merged, blocked, region_conflicts=(), named=None, pages=None):
    """Write the merged region patch to VISA_POLICY / POLICY_DOCUMENT / REVIEW_QUEUE.

    named: doc_ids that any section of the region's pages proposed before verification,
    given only when every page was read; a linked document outside it has vanished from
    the pages and its removal goes to review. A document whose quotes merely failed
    verification is still named and keeps its link. pages: source_id -> current markdown.
    """
    country = region
    documents = [doc.model_dump(exclude_none=True) for doc in merged.documents]
    entries = {}
    for entry in merged.policies:
        key = formal_extract._scope_key(entry.policy.applies_to_city, region)
        entries[key] = entry
    for item in blocked:
        key = formal_extract._scope_key(item["scope"], region)
        if key not in entries and (key != "*" or not entries):
            entries[key] = VisaPolicyEntry(
                policy=VisaPolicyData(applies_to_city=item["scope"] if key != "*" else None),
                evidence=[])
    if not entries and documents:
        entries["*"] = VisaPolicyEntry(policy=VisaPolicyData(), evidence=[])

    results = []
    for key, entry in entries.items():
        proposed = entry.policy.model_dump(exclude_none=True)
        conflicted = {c["field"]: c for c in region_conflicts
                      if c.get("field") in VISA_FIELDS
                      and formal_extract._scope_key(c.get("scope"), region) in (key, "*")}
        mine = _blocked_for_scope(blocked, key, region)
        blocked_fields, blocked_evidence, blocked_reasons = {}, [], []
        for item in mine:
            blocked_fields.update(item["fields"])
            blocked_evidence.extend(item["evidence"])
            blocked_reasons.extend(f"[{item['source_id']}] {r}" for r in item["reasons"])
        for field, conflict in conflicted.items():
            if field in proposed:
                blocked_fields[field] = proposed.pop(field)
                blocked_reasons.append(f"跨页面冲突 {field}：{conflict['values']}")
        clean = _row_values(proposed)
        blocked_row = _row_values(blocked_fields)
        if entry.policy.applies_to_city:
            blocked_row["applies_to_city"] = entry.policy.applies_to_city
        scope = _scope_of({"applies_to_city": entry.policy.applies_to_city})
        policy_id = _policy_id(country, scope)
        header = {"policy_id": policy_id, "country": country, "policy_type": "student",
                  "title": f"Student visa: {scope}"}
        evidence = [ev.model_dump() for ev in entry.evidence] + blocked_evidence
        existing = _row(connection, "SELECT * FROM VISA_POLICY WHERE policy_id=%s", (policy_id,))
        result = {"scope": scope, "policy_id": policy_id}

        if existing is None:
            if set(clean) - {"applies_to_city"}:
                columns = list(clean)
                connection.execute(f"""INSERT INTO VISA_POLICY
                    (policy_id, country, policy_type, title, {', '.join(columns)})
                    VALUES (%s,%s,%s,%s,{', '.join('%s' for _ in columns)})""",
                    [policy_id, country, "student", header["title"]] + [clean[c] for c in columns])
                _apply_documents(connection, policy_id, documents)
                _record_evidence(connection, policy_id, clean, entry.evidence, documents)
                result.update(result="inserted", fields=sorted(clean))
                if set(blocked_row) - {"applies_to_city"}:
                    result["review_id"] = _enqueue_review(
                        connection, table="VISA_POLICY", key=policy_id, action="UPDATE",
                        payload={"policy": {**blocked_row, **header}, "documents": []},
                        evidence=blocked_evidence,
                        reason=f"门控 {len(blocked_reasons)} 个错误：" + "；".join(blocked_reasons))
                else:
                    _reject_stale_reviews(connection, policy_id)
            else:
                result.update(result="review", review_id=_enqueue_review(
                    connection, table="VISA_POLICY", key=policy_id, action="CREATE",
                    payload={"policy": {**blocked_row, **header}, "documents": documents},
                    evidence=evidence,
                    reason=f"门控 {len(blocked_reasons)} 个错误：" + "；".join(blocked_reasons)))
            results.append(result)
            continue

        fill = {c: v for c, v in clean.items() if existing[c] is None}
        changed = {c: (existing[c], v) for c, v in clean.items()
                   if c != "remark" and existing[c] is not None and existing[c] != v}
        if fill:
            connection.execute(f"""UPDATE VISA_POLICY SET {', '.join(f'{c}=%s' for c in fill)}
                WHERE policy_id=%s""", [*fill.values(), policy_id])
        vanished = _vanished_documents(connection, policy_id, named, pages or {}) \
            if named is not None else []
        new_docs, changed_docs = _sync_documents(connection, policy_id, documents)
        _record_evidence(connection, policy_id, clean, entry.evidence, documents)
        result.update(filled=sorted(fill), linked_documents=len(new_docs))
        review_row = {**{c: after for c, (_, after) in changed.items()},
                      **{c: v for c, v in blocked_row.items()
                         if c != "applies_to_city" and existing[c] != v}}
        if review_row or changed_docs or vanished:
            parts = []
            if blocked_reasons:
                parts.append(f"门控 {len(blocked_reasons)} 个错误：" + "；".join(blocked_reasons))
            if changed:
                parts.append(f"字段变更 {len(changed)} 项：" + "; ".join(
                    f"{c}: {before!r} → {after!r}" for c, (before, after) in changed.items()))
            if changed_docs:
                parts.append("材料要求变更：" + "、".join(d["name_zh"] for d in changed_docs))
            gone = [d["name_zh"] for d in vanished if not d["still_quoted"]]
            missed = [d["name_zh"] for d in vanished if d["still_quoted"]]
            if gone:
                parts.append("页面不再列出的材料（批准后移除）：" + "、".join(gone))
            if missed:
                parts.append("本次未抽到、但原引用仍在页面上的材料（批准后同样移除，请先核对是否漏抽）："
                             + "、".join(missed))
            result.update(result="review", review_id=_enqueue_review(
                connection, table="VISA_POLICY", key=policy_id, action="UPDATE",
                payload={"policy": {**review_row, **header}, "documents": changed_docs,
                         "removed_documents": vanished},
                evidence=evidence, reason="；".join(parts)),
                changed={c: {"before": b, "after": a} for c, (b, a) in changed.items()})
        else:
            result["result"] = "updated" if fill or new_docs else "no_change"
            _reject_stale_reviews(connection, policy_id)
        results.append(result)
    return results


def apply_review(connection, review_id, *, decided_by, note=None,
                 tuition_amount=None, tuition_currency=None, fx_selections=None):
    """Approve one REVIEW_QUEUE row: apply its payload to the domain table."""
    row = _row(connection, "SELECT * FROM admin.review_queue WHERE review_id=%s", (review_id,))
    if not row:
        raise ValueError(f"没有待处理的审核项：{review_id}")
    if row["domain_table"] == "FX_POLICY":
        from . import fx_pipeline
        return fx_pipeline.apply_review(connection, row, decided_by=decided_by, note=note,
                                        selected_fields=fx_selections)
    if row["domain_table"] == "TUITION":
        from . import tuition_pipeline
        return tuition_pipeline.apply_review(
            connection, row, decided_by=decided_by, note=note,
            selected_amount=tuition_amount, selected_currency=tuition_currency)
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
        connection.execute(f"""INSERT INTO VISA_POLICY
            (policy_id, country, policy_type, title, {', '.join(field_columns)})
            VALUES (%s,%s,%s,%s,{', '.join('%s' for _ in field_columns)})""",
            [policy_id, policy.get("country", "unknown"), policy.get("policy_type", "student"),
             policy.get("title", policy_id)] + [policy[c] for c in field_columns])
    elif field_columns:
        connection.execute(f"""UPDATE VISA_POLICY SET {', '.join(f'{c}=%s' for c in field_columns)}
            WHERE policy_id=%s""", [policy[c] for c in field_columns] + [policy_id])
    _apply_documents(connection, policy_id, payload.get("documents"))
    _remove_documents(connection, policy_id, payload.get("removed_documents"))
    _record_evidence(connection, policy_id, {c: policy[c] for c in field_columns},
                     json.loads(row["evidence_json"] or "[]"), payload.get("documents"))
    connection.execute("DELETE FROM admin.review_queue WHERE review_id=%s", (review_id,))
    return {"review_id": review_id, "action": row["action"], "policy_id": policy_id,
            "applied_fields": field_columns}


def reject_review(connection, review_id, *, decided_by, note):
    cur = connection.execute("DELETE FROM admin.review_queue WHERE review_id=%s", (review_id,))
    if cur.rowcount == 0:
        raise ValueError(f"没有待处理的审核项：{review_id}")
    return {"review_id": review_id, "status": "REJECTED"}


# ---------- per-page capture ----------

def _extract_sections(source, paths, markdown, previous_md, profile, full, settings, client_box,
                      extractor=None):
    extractor = extractor or formal_extract
    patch_class = getattr(extractor, "PATCH", VisaPatch)
    split = getattr(extractor, "split_sections", incremental.split_sections)
    sections = split(markdown)
    old_sections = []
    if previous_md:
        old_file = _snapshot_file(paths, previous_md)
        if old_file.is_file():
            old_sections = split(old_file.read_text(encoding="utf-8"))
    affected, _removed = incremental.changed_sections(old_sections, sections)
    if full:
        affected = {section.key for section in sections}
    cache = _load_cache(paths)
    patches, reused, model_calls, tokens = [], [], 0, [0, 0]
    for section in sections:
        cache_key = f"{section.content_hash}:{profile}"
        if section.key not in affected and cache_key in cache:
            patches.append(patch_class.model_validate_json(cache[cache_key]))
            reused.append(section.key)
            continue
        if client_box[0] is None:
            client_box[0], client_box[1] = formal_extract.legacy_extract._client()
        patch, usage = extractor.extract_section(section, source, client_box[0], client_box[1])
        model_calls += 1
        tokens[0] += usage[0] or 0
        tokens[1] += usage[1] or 0
        patches.append(patch)
        # 逐条立即缓存：抽取是付费的，崩溃/合并冲突不应丢失已完成章节
        # （缓存按 profile 隔离，修好逻辑后重跑零成本复用）
        _save_cache_entry(paths, section.content_hash, profile, patch.model_dump_json())
    return patches, len(reused), model_calls, tokens


def _capture(source, paths, settings, client_box, *, fresh, full, extractor=None):
    """Snapshot + extraction for one page. Returns a capture dict.

    fresh=False reuses the last snapshot without touching the network (sibling
    pages of the region that were not asked to be re-checked). extractor is a
    module with extraction_profile / extract_section / PATCH (default: visa) and
    optionally source_notes(source), the per-source input of its cache profile.
    """
    extractor = extractor or formal_extract
    source_id = source["id"]
    state = _load_state(paths, source_id)
    notes = (extractor.source_notes(source) if hasattr(extractor, "source_notes")
             else source.get("extraction_notes"))
    now = datetime.now(timezone.utc)
    epoch = state.get("cache_epoch", "initial")
    status = "CACHED"
    probe = None
    html = ""
    previous_md = state.get("snapshot_md")
    previous_hash = state.get("content_hash")
    can_reuse = bool(previous_md and (paths.data / previous_md).is_file() and state.get("snapshot_id"))

    if fresh:
        last_full = storage_timestamp(state.get("last_full_at"))
        if full or last_full is None or now - last_full >= timedelta(days=7):
            full = True
            epoch = _id("epoch")
        profile = extractor.extraction_profile(settings, epoch, notes)
        due = storage_timestamp(state.get("next_check_at"))
        if not full and due and now < due and state.get("last_extraction_profile") == profile:
            fresh, status = False, "NOT_DUE"
    if not fresh:
        if not can_reuse:
            return None
        full = False
        profile = extractor.extraction_profile(settings, epoch, notes)
        markdown = _snapshot_file(paths, previous_md).read_text(encoding="utf-8")
        snapshot_id, new_md = state["snapshot_id"], previous_md
    else:
        crawl.validate_source(source)
        probe = incremental.conditional_probe(source, state)
        if can_reuse and not full and probe.result in ("NOT_MODIFIED", "BODY_UNCHANGED"):
            markdown = _snapshot_file(paths, previous_md).read_text(encoding="utf-8")
            snapshot_id, new_md = state["snapshot_id"], previous_md
            status = probe.result if state.get("last_extraction_profile") == profile else "REPROCESS"
        else:
            result = asyncio.run(crawl.fetch_page(source))
            if not result.success:
                raise crawl.CrawlError("upstream_error", result.error_message or "Crawl4AI 抓取失败")
            final_url = getattr(result, "url", source["url"])
            if urlparse(final_url).scheme != "https" or urlparse(final_url).hostname != source["allowed_host"]:
                raise ValueError("浏览器被重定向到未登记的主机")
            markdown = crawl.extract_markdown(result)
            if len(" ".join(markdown.split())) < 200 or crawl.detect_blocked_page(markdown):
                raise ValueError("浏览器内容为空、过短或疑似拦截页")
            content_hash = incremental.normalized_hash(markdown)
            if can_reuse and content_hash == previous_hash and not full:
                snapshot_id, new_md = state["snapshot_id"], previous_md
                status = ("BODY_UNCHANGED" if state.get("last_extraction_profile") == profile
                          else "REPROCESS")
            else:
                status, html = "CHANGED", result.html or ""
                snapshot_id = _id("snap")
                snapshot_dir = paths.snapshots / source_id
                snapshot_dir.mkdir(parents=True, exist_ok=True)
                (snapshot_dir / f"{snapshot_id}.md").write_text(markdown, encoding="utf-8")
                if html:
                    (snapshot_dir / f"{snapshot_id}.html").write_text(html, encoding="utf-8")
                new_md = str((snapshot_dir / f"{snapshot_id}.md").relative_to(paths.data))

    capture = {"source": source, "status": status, "snapshot_id": snapshot_id,
               "markdown": markdown, "patches": [], "reused_sections": 0,
               "model_calls": 0, "tokens": [0, 0], "fresh": fresh}
    patches, reused, calls, tokens = _extract_sections(
        source, paths, markdown, previous_md, profile, full, settings, client_box, extractor)
    capture.update(patches=patches, reused_sections=reused, model_calls=calls, tokens=tokens)

    if fresh:
        checked = datetime.now(timezone.utc)
        new_state = dict(state)
        new_state.update({
            "etag": probe.etag if probe and probe.etag else state.get("etag"),
            "last_modified": (probe.last_modified if probe and probe.last_modified
                              else state.get("last_modified")),
            "raw_body_hash": (probe.raw_body_hash if probe and probe.raw_body_hash
                              else state.get("raw_body_hash")),
            "last_checked_at": checked.isoformat(),
            "next_check_at": _next_due(source, checked),
            "last_result": status if status in UNCHANGED else "CHANGED",
        })
        if status not in UNCHANGED:
            new_state.update({
                "cache_epoch": epoch,
                "last_extraction_profile": profile,
                "snapshot_id": snapshot_id,
                "snapshot_md": new_md,
                "content_hash": incremental.normalized_hash(markdown),
                "last_complete_at": checked.isoformat(),
                "last_full_at": checked.isoformat() if full else state.get("last_full_at"),
            })
        capture["state"] = new_state
    return capture


def _page_patch(capture, region):
    """Merge one page's sections, derive document flags and gate it against its snapshot.

    Each section's values are checked against the snapshot first: a value without
    verbatim support is dropped (reported as a warning) before merging, so it can
    neither win nor turn a supported value from another section into a conflict.
    """
    source = capture["source"]
    markdown = capture["markdown"]
    verified, discarded = [], []
    for section_patch in capture["patches"]:
        section_report = gate.verify_visa_evidence(section_patch, markdown)
        clean, _ = split_blocked(section_patch, section_report)
        verified.append(formal_extract.mark_unstated_conditions(clean, markdown))
        discarded += [p for p in section_report.problems if p["severity"] == "error"]
    conflicts = []
    try:
        patch = formal_extract.merge_sections(verified, conflicts=conflicts, region=region)
    except ExtractionError:
        patch = VisaPatch(policies=[], documents=[])
    patch = formal_extract.derive_document_flags(patch)
    patch = formal_extract.attribute(patch, source["id"], capture.get("snapshot_id"))
    report = gate.run_gate_visa(patch, markdown, source, {"excerpt_chars": len(markdown)})
    for problem in discarded:
        report.problem(problem["check"], "warning", f"已丢弃无原文支撑的章节取值：{problem['reason']}",
                       problem.get("suggested_action"),
                       **{k: problem[k] for k in ("field", "value", "quote", "scope") if k in problem})
    for conflict in conflicts:
        report.problem("conflict", "error", conflict["reason"],
                       "人工核对页面原文，在审核中选定取值",
                       field=conflict.get("field"), value=_json(conflict.get("values")),
                       scope=conflict.get("scope"))
    return patch, report


# ---------- scan orchestration ----------

def _scan_region(region, *, paths, fresh_ids, full, sources=None, reapply=False):
    sources = sources if sources is not None else load_sources()
    members = _region_sources(region, sources)
    if not members:
        raise ValueError(f"区域 {region} 没有启用的 visa_policy 来源")
    settings = config.llm_settings()
    client_box = [None, settings]
    captures, page_results = [], []
    for source in members:
        fresh = source["id"] in fresh_ids
        try:
            capture = _capture(source, paths, settings, client_box, fresh=fresh,
                               full=full and fresh)
        except Exception as exc:
            if len(fresh_ids) == 1 and fresh:
                raise
            page_results.append({"source_id": source["id"], "status": "FAILED",
                                 "error": f"{type(exc).__name__}: {exc}"})
            try:
                capture = _capture(source, paths, settings, client_box, fresh=False, full=False)
            except Exception:
                capture = None
            if capture is None:
                continue
        if capture is None:
            page_results.append({"source_id": source["id"], "status": "MISSING",
                                 "error": "尚无快照；用 scan-region 或 scan 该来源先抓取一次"})
            continue
        captures.append(capture)

    changed = [c for c in captures if c["fresh"] and c["status"] not in UNCHANGED]
    totals = {"model_calls": sum(c["model_calls"] for c in captures),
              "prompt_tokens": sum(c["tokens"][0] for c in captures),
              "completion_tokens": sum(c["tokens"][1] for c in captures),
              "reused_sections": sum(c["reused_sections"] for c in captures)}
    if not changed and not reapply:
        for capture in captures:
            if "state" in capture:
                _save_state(paths, capture["source"]["id"], capture["state"])
        from . import publication
        with storage.session(paths, readonly=False) as connection:
            publications = publication.reverify_region(connection, paths, region, sources)
        return {"result": "no_change", "region": region, "applied": False,
                "pages": page_results + [{"source_id": c["source"]["id"], "status": c["status"]}
                                         for c in captures], **totals,
                "publications": publications}

    clean_patches, blocked, reports = [], [], []
    for capture in captures:
        patch, report = _page_patch(capture, region)
        clean, page_blocked = split_blocked(patch, report, capture["source"]["id"])
        clean_patches.append(clean)
        blocked.extend(page_blocked)
        reports.append((capture, report))
        page_results.append({
            "source_id": capture["source"]["id"], "status": capture["status"],
            "snapshot_id": capture["snapshot_id"], "model_calls": capture["model_calls"],
            "reused_sections": capture["reused_sections"],
            "policies": len(patch.policies), "documents": len(patch.documents),
            "errors": [p["reason"] for p in report.problems if p["severity"] == "error"],
            "warnings": [p["reason"] for p in report.problems if p["severity"] == "warning"]})

    region_conflicts = []
    try:
        merged = formal_extract.merge_sections(clean_patches, conflicts=region_conflicts, region=region)
    except ExtractionError:
        merged = VisaPatch(policies=[], documents=[])
    if not merged.policies and not merged.documents and not blocked:
        raise ValueError(f"区域 {region} 所有页面都没有抽取到签证政策字段；保留已入库知识并检查覆盖范围")
    region_report = gate.GateReport()
    for conflict in region_conflicts:
        region_report.problem("conflict", "error", f"跨页面：{conflict['reason']}",
                              "人工核对各页面原文，在审核中选定取值",
                              field=conflict.get("field"), value=_json(conflict.get("values")),
                              scope=conflict.get("scope"))

    named = {_document_link(d.model_dump())["doc_id"]
             for c in captures for p in c["patches"] for d in p.documents if d.code.strip()} \
        if len(captures) == len(members) else None
    pages = {c["source"]["id"]: c["markdown"] for c in captures}
    with storage.session(paths, readonly=False) as connection:
        applied = _apply_region(connection, region, merged, blocked, region_conflicts, named, pages)
        gate_issue_ids = []
        for capture, report in reports:
            gate_issue_ids += _persist_gate_issues(connection, capture["source"],
                                                   capture["snapshot_id"], report)
        gate_issue_ids += _persist_gate_issues(connection, {"id": f"region:{region}"}, None,
                                               region_report)
    for capture in captures:
        if "state" in capture:
            _save_state(paths, capture["source"]["id"], capture["state"])
    from . import publication
    with storage.session(paths, readonly=False) as connection:
        publications = publication.reverify_region(connection, paths, region, sources)
    all_problems = [p for _, r in reports for p in r.problems] + region_report.problems
    return {"result": "applied", "region": region, "applied": True, "pages": page_results,
            **totals, "policies": applied, "gate_issue_ids": gate_issue_ids,
            "publications": publications,
            "gate": {"errors": [p["reason"] for p in all_problems if p["severity"] == "error"],
                     "warnings": [p["reason"] for p in all_problems if p["severity"] == "warning"]}}


def _guard(paths):
    if paths.environment == "production":
        raise ValueError("正式环境采集须等待审核/发布服务接入")


def scan(source_id, *, paths=None, full=False):
    """Re-check one registered source and rewrite its region's VISA_POLICY rows.

    The other pages of the region contribute from their last snapshot (no network,
    model calls only for sections missing from the cache).
    """
    paths = paths or config.PATHS
    _guard(paths)
    _validate_source_id(source_id)
    sources = load_sources()
    if source_id not in sources:
        raise KeyError(f"sources.json 中没有来源：{source_id}")
    source = sources[source_id]
    if source.get("schema_type") == "fx_policy":
        from . import fx_pipeline
        return fx_pipeline.scan(source_id, paths=paths, full=full)
    if source.get("schema_type") == "tuition":
        from . import tuition_pipeline
        return tuition_pipeline.scan(source_id, paths=paths, full=full)
    if source.get("schema_type") != "visa_policy":
        raise ValueError("新管道目前只支持 visa_policy / fx_policy / tuition；其他领域待专用抽取器接入")
    crawl.validate_source(source)
    region = source.get("region") or "unknown"
    outcome = _scan_region(region, paths=paths, fresh_ids={source_id}, full=full, sources=sources)
    page = next((p for p in outcome["pages"] if p["source_id"] == source_id), {})
    outcome["source_id"] = source_id
    outcome["snapshot_id"] = page.get("snapshot_id")
    if not outcome["applied"]:
        status = page.get("status", "NOT_DUE")
        outcome["result"] = status.lower()
    return outcome


def scan_region(region, *, paths=None, full=False, reapply=False):
    """Re-check every visa_policy page of a region (or 'all') and rewrite its rows.

    reapply=True skips the network and re-merges/gates/writes from the stored
    snapshots and cached section extractions (after a merge or gate rule change).
    """
    paths = paths or config.PATHS
    _guard(paths)
    sources = load_sources()
    regions = visa_regions(sources) if region == "all" else [region]
    outcomes = []
    for name in regions:
        fresh = set() if reapply else {s["id"] for s in _region_sources(name, sources)}
        outcomes.append(_scan_region(name, paths=paths, fresh_ids=fresh, full=full and not reapply,
                                     sources=sources, reapply=reapply))
    return outcomes[0] if region != "all" else {"result": "applied", "regions": outcomes}
