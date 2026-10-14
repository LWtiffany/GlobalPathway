"""VISA_POLICY 发布面：人工发布封存修订，Agent2 只读已发布修订。

publish   当前工作行（字段 + 材料 + 逐字证据）通过发布前检查后封存为不可改的
          POLICY_REVISION，POLICY_PUBLICATION 指向它；事实与已发布修订相同时只刷新核验时间。
withdraw  指针置为 WITHDRAWN，修订与历史保留。
reverify  扫描后自动执行：工作行事实与已发布修订一致、证据仍成立时刷新 verified_at；
          不一致时已发布修订保持不变，等待人工重新发布（verified_at 不再前进，超 SLA 后变 stale）。
search / get  Agent2 的只读接口，返回统一信封与可用性，不暴露工作表。
"""
import hashlib
import json
import uuid
from datetime import datetime, timezone

from . import config, gate, storage
from .formal_pipeline import VISA_FIELD_COLUMNS, load_sources, visa_regions

SCHEMA_VERSION = "visa-publication-1"
DEFAULT_SLA_HOURS = 168
CITABLE_COLUMNS = tuple(c for c in VISA_FIELD_COLUMNS if c != "remark")


class PublishError(ValueError):
    pass


def _now():
    return datetime.now(timezone.utc)


def _parse(value):
    if not value:
        return None
    parsed = datetime.fromisoformat(str(value))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _sha256(data):
    return hashlib.sha256(data).hexdigest()


def _rows(connection, sql, parameters=()):
    return [dict(r) for r in connection.execute(sql, parameters)]


def _row(connection, sql, parameters=()):
    result = connection.execute(sql, parameters).fetchone()
    return dict(result) if result else None


def _snapshot_path(paths, source_id, snapshot_id):
    path = paths.snapshots / source_id / f"{snapshot_id}.md"
    if not path.resolve().is_relative_to(paths.snapshots.resolve()):
        raise ValueError("快照路径越过环境目录")
    return path


def _required(value):
    return None if value is None else bool(value)


# ---------- sealing ----------

def build(connection, paths, policy_id, sources):
    """Seal the working row. Returns (payload, facts_hash, problems)."""
    row = _row(connection, "SELECT * FROM VISA_POLICY WHERE policy_id=?", (policy_id,))
    if row is None:
        raise PublishError(f"没有签证政策：{policy_id}")
    evidence = {}
    for ev in _rows(connection, """SELECT * FROM POLICY_EVIDENCE WHERE policy_id=?
                                   ORDER BY target, kind, source_id, quote""", (policy_id,)):
        evidence.setdefault(ev["target"], []).append(ev)
    texts, problems, used = {}, [], set()

    def text_of(source_id, snapshot_id):
        key = (source_id, snapshot_id)
        if key not in texts:
            path = _snapshot_path(paths, source_id, snapshot_id)
            texts[key] = path.read_text(encoding="utf-8") if path.is_file() else None
        return texts[key]

    def seal(items, label):
        sealed = []
        for ev in items:
            text = text_of(ev["source_id"], ev["snapshot_id"])
            check = gate.condition_quote_in_markdown if ev["kind"] == "condition" else gate.quote_in_markdown
            if text is None:
                problems.append(f"{label}：快照 {ev['snapshot_id']} 不存在")
            elif not check(ev["quote"], text):
                problems.append(f"{label}：引用在快照 {ev['snapshot_id']} 中找不到")
            used.add(ev["source_id"])
            sealed.append({k: ev[k] for k in ("evidence_id", "kind", "source_id", "snapshot_id", "quote")})
        return sealed

    fields = {c: row[c] for c in CITABLE_COLUMNS if row[c] is not None}
    field_evidence = {}
    for column in fields:
        items = evidence.get(f"field:{column}", [])
        if not items:
            problems.append(f"字段 {column} 没有逐字证据")
            continue
        field_evidence[column] = seal(items, f"字段 {column}")

    documents, facts_documents = [], []
    for doc in _rows(connection, """SELECT d.doc_id, d.code, d.name_zh, pd.required, pd."condition"
            FROM POLICY_DOCUMENT pd JOIN DOCUMENT d USING (doc_id)
            WHERE pd.policy_id=? AND pd.policy_domain='visa_policy'
            ORDER BY d.code, d.name_zh""", (policy_id,)):
        items = evidence.get(f"document:{doc['doc_id']}", [])
        label = f"材料 {doc['name_zh']}"
        if not any(i["kind"] == "value" for i in items):
            problems.append(f"{label} 没有逐字证据")
        if doc["condition"] and not any(i["kind"] == "condition" for i in items):
            problems.append(f"{label} 的适用条件没有逐字证据")
        fact = {"doc_id": doc["doc_id"], "code": doc["code"], "name_zh": doc["name_zh"],
                "required": _required(doc["required"]),
                "condition": json.loads(doc["condition"]) if doc["condition"] else None}
        facts_documents.append(fact)
        documents.append({**fact, "evidence": seal(items, label)})

    source_meta, snapshots = {}, {}
    for source_id in sorted(used):
        source = sources.get(source_id)
        if source is None:
            problems.append(f"来源 {source_id} 已不在 sources.json 中")
            continue
        source_meta[source_id] = {
            "name": source.get("name"), "url": source["url"],
            "authority": source.get("authority", "official"),
            "freshness_sla_hours": source.get("freshness_sla_hours") or DEFAULT_SLA_HOURS}
    for items in [*field_evidence.values(), *(d["evidence"] for d in documents)]:
        for ev in items:
            path = _snapshot_path(paths, ev["source_id"], ev["snapshot_id"])
            if ev["snapshot_id"] not in snapshots and path.is_file():
                snapshots[ev["snapshot_id"]] = {"source_id": ev["source_id"],
                                                "sha256": _sha256(path.read_bytes())}

    policy = {k: row[k] for k in ("policy_id", "country", "policy_type", "title")}
    facts = {"policy": policy, "fields": fields, "documents": facts_documents}
    payload = {"schema_version": SCHEMA_VERSION, **facts, "documents": documents,
               "field_evidence": field_evidence, "notes": row["remark"],
               "sources": source_meta, "snapshots": snapshots}
    return payload, _sha256(_canonical(facts).encode("utf-8")), problems


def _verified_at(paths, source_ids):
    """Oldest successful check among the sources the evidence comes from."""
    state_path = paths.data / "check_state.json"
    state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.is_file() else {}
    times, problems = [], []
    for source_id in source_ids:
        checked = _parse(state.get(source_id, {}).get("last_checked_at"))
        if checked is None:
            problems.append(f"来源 {source_id} 没有成功核验记录")
        else:
            times.append(checked)
    return (min(times).isoformat() if times and not problems else None), problems


def _open_gate_issues(connection, country, sources):
    ids = [s["id"] for s in sources.values()
           if s.get("schema_type") == "visa_policy" and (s.get("region") or "unknown") == country]
    ids.append(f"region:{country}")
    placeholders = ",".join("?" for _ in ids)
    return _rows(connection, f"""SELECT issue_id, source_id, severity, field, reason FROM GATE_ISSUE
        WHERE status='OPEN' AND source_id IN ({placeholders})
        ORDER BY source_id, severity, field""", ids)


def _event(connection, policy_id, action, *, actor, revision_id=None, previous=None,
           reason=None, detail=None):
    connection.execute("""INSERT INTO PUBLICATION_EVENT
        (event_id, policy_id, action, revision_id, previous_revision_id, actor, reason, detail_json, at)
        VALUES (?,?,?,?,?,?,?,?,?)""",
        (f"pubev-{uuid.uuid4().hex}", policy_id, action, revision_id, previous, actor, reason,
         _canonical(detail) if detail is not None else None, _now().isoformat()))


def _current(connection, policy_id):
    return _row(connection, """SELECT p.*, r.facts_hash, r.revision_no FROM POLICY_PUBLICATION p
        JOIN POLICY_REVISION r USING (revision_id) WHERE p.policy_id=?""", (policy_id,))


def _source_ids(payload):
    return sorted(payload["sources"]) or sorted(
        {ev["source_id"] for items in payload["field_evidence"].values() for ev in items})


def _check(connection, paths, policy_id, sources, ack_warnings):
    payload, facts_hash, problems = build(connection, paths, policy_id, sources)
    verified_at, stale = _verified_at(paths, _source_ids(payload))
    problems += stale
    if verified_at is None and not stale:
        problems.append("没有任何可追溯的证据来源")
    for review in _rows(connection, """SELECT review_id FROM REVIEW_QUEUE
            WHERE record_key=? AND status='OPEN'""", (policy_id,)):
        problems.append(f"审核项 {review['review_id']} 尚未处理")
    issues = _open_gate_issues(connection, payload["policy"]["country"], sources)
    errors = [i for i in issues if i["severity"] == "error"]
    warnings = [i for i in issues if i["severity"] == "warning"]
    problems += [f"门控错误未解决（{i['source_id']}）：{i['reason']}" for i in errors]
    if warnings and not ack_warnings:
        problems.append(f"{len(warnings)} 条门控警告尚未确认；逐条核对后加 --ack-warnings")
    return payload, facts_hash, verified_at, problems, warnings


def _summary(payload):
    def doc_line(doc):
        if doc["required"] is None:
            state = "条件未知"
        elif not doc["required"]:
            state = "仅提及"
        else:
            state = f"条件 {_canonical(doc['condition'])}" if doc["condition"] else "必需"
        return f"{doc['code']} {doc['name_zh']}：{state}"
    return {"fields": payload["fields"], "documents": [doc_line(d) for d in payload["documents"]],
            "sources": sorted(payload["sources"])}


def preview(target, *, paths=None, sources=None):
    """What publish would seal for each policy, and why it would be refused. Writes nothing."""
    paths = paths or config.PATHS
    sources = sources if sources is not None else load_sources()
    results = []
    with storage.session(paths) as connection:
        for policy_id in _targets(connection, target, sources):
            payload, facts_hash, verified_at, problems, warnings = _check(
                connection, paths, policy_id, sources, ack_warnings=True)
            current = _current(connection, policy_id)
            results.append({
                "policy_id": policy_id, "title": payload["policy"]["title"],
                "country": payload["policy"]["country"], "verified_at": verified_at,
                "published": bool(current and current["status"] == "ACTIVE"
                                  and current["facts_hash"] == facts_hash),
                "problems": problems,
                "warnings": [f"[{i['source_id']}] {i['field'] or '-'}：{i['reason']}" for i in warnings],
                **_summary(payload)})
    return results


def _publish_one(connection, paths, policy_id, sources, *, actor, reason, ack_warnings):
    payload, facts_hash, verified_at, problems, warnings = _check(
        connection, paths, policy_id, sources, ack_warnings)
    result = {"policy_id": policy_id, "title": payload["policy"]["title"],
              "warnings": [f"[{i['source_id']}] {i['reason']}" for i in warnings]}
    if problems:
        return {**result, "result": "refused", "problems": problems}

    current = _current(connection, policy_id)
    if current and current["status"] == "ACTIVE" and current["facts_hash"] == facts_hash:
        newer = max(_parse(current["verified_at"]), _parse(verified_at)).isoformat()
        connection.execute("UPDATE POLICY_PUBLICATION SET verified_at=? WHERE policy_id=?",
                           (newer, policy_id))
        _event(connection, policy_id, "REVERIFY", actor=actor, revision_id=current["revision_id"],
               reason=reason or "发布内容未变化，刷新核验时间")
        return {**result, "result": "unchanged", "revision_id": current["revision_id"],
                "verified_at": newer}

    revision_no = connection.execute("SELECT COALESCE(MAX(revision_no), 0) + 1 FROM POLICY_REVISION "
                                     "WHERE policy_id=?", (policy_id,)).fetchone()[0]
    revision_id = f"rev-{uuid.uuid4().hex}"
    now = _now().isoformat()
    connection.execute("""INSERT INTO POLICY_REVISION
        (revision_id, policy_id, revision_no, facts_hash, payload_json, created_by, created_at)
        VALUES (?,?,?,?,?,?,?)""",
        (revision_id, policy_id, revision_no, facts_hash, _canonical(payload), actor, now))
    connection.execute("""INSERT INTO POLICY_PUBLICATION
        (policy_id, revision_id, status, published_by, published_at, verified_at)
        VALUES (?,?, 'ACTIVE', ?,?,?)
        ON CONFLICT(policy_id) DO UPDATE SET revision_id=excluded.revision_id, status='ACTIVE',
            published_by=excluded.published_by, published_at=excluded.published_at,
            verified_at=excluded.verified_at, withdrawn_by=NULL, withdrawn_at=NULL,
            withdraw_reason=NULL""",
        (policy_id, revision_id, actor, now, verified_at))
    _event(connection, policy_id, "PUBLISH", actor=actor, revision_id=revision_id,
           previous=current["revision_id"] if current else None, reason=reason,
           detail={"facts_hash": facts_hash,
                   "acknowledged_warnings": [i["issue_id"] for i in warnings]})
    return {**result, "result": "published", "revision_id": revision_id,
            "revision_no": revision_no, "verified_at": verified_at,
            "documents": len(payload["documents"]), "fields": sorted(payload["fields"])}


def _targets(connection, target, sources):
    if target == "all" or target in visa_regions(sources):
        sql = "SELECT policy_id FROM VISA_POLICY" + ("" if target == "all" else " WHERE country=?")
        return [r["policy_id"] for r in _rows(connection, sql + " ORDER BY country, title",
                                              () if target == "all" else (target,))]
    if _row(connection, "SELECT 1 FROM VISA_POLICY WHERE policy_id=?", (target,)):
        return [target]
    raise PublishError(f"未知的发布目标：{target}（区域 / all / policy_id）")


def publish(target, *, actor, reason=None, ack_warnings=False, paths=None, sources=None):
    """Publish one policy, a region or 'all'. Each policy passes or is refused on its own."""
    paths = paths or config.PATHS
    sources = sources if sources is not None else load_sources()
    with storage.session(paths, readonly=False) as connection:
        return [_publish_one(connection, paths, policy_id, sources, actor=actor, reason=reason,
                             ack_warnings=ack_warnings)
                for policy_id in _targets(connection, target, sources)]


def withdraw(policy_id, *, actor, reason, paths=None):
    if not (reason or "").strip():
        raise PublishError("撤回必须填写理由")
    paths = paths or config.PATHS
    with storage.session(paths, readonly=False) as connection:
        current = _current(connection, policy_id)
        if not current or current["status"] != "ACTIVE":
            raise PublishError(f"{policy_id} 没有生效中的发布")
        now = _now().isoformat()
        connection.execute("""UPDATE POLICY_PUBLICATION SET status='WITHDRAWN', withdrawn_by=?,
            withdrawn_at=?, withdraw_reason=? WHERE policy_id=?""", (actor, now, reason, policy_id))
        _event(connection, policy_id, "WITHDRAW", actor=actor, revision_id=current["revision_id"],
               reason=reason)
    return {"policy_id": policy_id, "revision_id": current["revision_id"], "status": "WITHDRAWN"}


def reverify_region(connection, paths, country, sources):
    """After a scan: refresh verified_at of ACTIVE publications whose facts did not change."""
    results = []
    for pub in _rows(connection, """SELECT p.policy_id FROM POLICY_PUBLICATION p
            JOIN VISA_POLICY v USING (policy_id) WHERE p.status='ACTIVE' AND v.country=?""", (country,)):
        policy_id = pub["policy_id"]
        current = _current(connection, policy_id)
        _payload, facts_hash, verified_at, problems, _warnings = _check(
            connection, paths, policy_id, sources, ack_warnings=True)
        if facts_hash != current["facts_hash"]:
            results.append({"policy_id": policy_id, "result": "outdated",
                            "reason": "工作行事实与已发布修订不同，需人工重新发布"})
        elif problems:
            results.append({"policy_id": policy_id, "result": "not_reverified",
                            "reason": "；".join(problems)})
        elif _parse(verified_at) > _parse(current["verified_at"]):
            connection.execute("UPDATE POLICY_PUBLICATION SET verified_at=? WHERE policy_id=?",
                               (verified_at, policy_id))
            _event(connection, policy_id, "REVERIFY", actor="scan", revision_id=current["revision_id"],
                   reason="扫描确认已发布事实未变化")
            results.append({"policy_id": policy_id, "result": "reverified", "verified_at": verified_at})
        else:
            results.append({"policy_id": policy_id, "result": "unchanged"})
    return results


# ---------- Agent2 read interface ----------

def _availability(pub, revision, payload, now, paths):
    if revision["revision_id"] != pub["revision_id"]:
        return "superseded", "该修订已被新发布的修订取代"
    if pub["status"] == "WITHDRAWN":
        return "withdrawn", pub.get("withdraw_reason") or "已撤回"
    fields = payload["fields"]
    today = now.date().isoformat()
    if fields.get("effective_from") and str(fields["effective_from"]) > today:
        return "not_yet_effective", f"{fields['effective_from']} 起生效"
    if fields.get("effective_to") and str(fields["effective_to"]) < today:
        return "expired", f"{fields['effective_to']} 已失效"
    sla = min((s["freshness_sla_hours"] for s in payload["sources"].values()), default=DEFAULT_SLA_HOURS)
    age = (now - _parse(pub["verified_at"])).total_seconds() / 3600
    if age > sla:
        return "stale", f"最近核验距今 {age:.0f} 小时，超过 {sla} 小时"
    for snapshot_id, meta in payload["snapshots"].items():
        path = _snapshot_path(paths, meta["source_id"], snapshot_id)
        if not path.is_file() or _sha256(path.read_bytes()) != meta["sha256"]:
            return "missing_evidence", f"证据快照 {snapshot_id} 缺失或被改动"
    return "available", None


def _envelope(pub, revision, now, paths):
    payload = json.loads(revision["payload_json"])
    availability, reason = _availability(pub, revision, payload, now, paths)
    evidence = [{**ev, "target": f"field:{field}"}
                for field, items in payload["field_evidence"].items() for ev in items]
    evidence += [{**ev, "target": f"document:{doc['doc_id']}"}
                 for doc in payload["documents"] for ev in doc["evidence"]]
    for ev in evidence:
        source = payload["sources"].get(ev["source_id"], {})
        ev["source_url"] = source.get("url")
        ev["source_name"] = source.get("name")
    policy = payload["policy"]
    return {
        "record_id": policy["policy_id"],
        "revision_id": revision["revision_id"],
        "revision_no": revision["revision_no"],
        "domain": "visa_policy",
        "scope": {"country": policy["country"], "policy_type": policy["policy_type"],
                  "applies_to_city": payload["fields"].get("applies_to_city")},
        "title": policy["title"],
        "payload": {"fields": payload["fields"], "documents": payload["documents"],
                    "notes": payload["notes"]},
        "evidence": evidence,
        "sources": payload["sources"],
        "source_type": "official" if all(s["authority"] == "official"
                                         for s in payload["sources"].values()) else "mixed",
        "verified_at": pub["verified_at"],
        "published_at": pub["published_at"],
        "effective_from": payload["fields"].get("effective_from"),
        "effective_to": payload["fields"].get("effective_to"),
        "availability": availability,
        "availability_reason": reason,
        "schema_version": payload["schema_version"],
        "synthetic": False,
    }


def search(country=None, policy_type="student", *, now=None, paths=None):
    """Current ACTIVE publications (stale ones included, flagged by availability)."""
    paths = paths or config.PATHS
    now = now or _now()
    with storage.session(paths) as connection:
        rows = _rows(connection, """SELECT p.*, r.revision_no, r.payload_json FROM POLICY_PUBLICATION p
            JOIN POLICY_REVISION r USING (revision_id) JOIN VISA_POLICY v USING (policy_id)
            WHERE p.status='ACTIVE' AND (? IS NULL OR v.country=?) AND v.policy_type=?
            ORDER BY v.country, v.title""", (country, country, policy_type))
    return [_envelope(row, row, now, paths) for row in rows]


def get(policy_id, revision_id, *, now=None, paths=None):
    """One revision as it was published, with its availability right now (None if unknown)."""
    paths = paths or config.PATHS
    now = now or _now()
    with storage.session(paths) as connection:
        revision = _row(connection, "SELECT * FROM POLICY_REVISION WHERE revision_id=? AND policy_id=?",
                        (revision_id, policy_id))
        pub = _row(connection, "SELECT * FROM POLICY_PUBLICATION WHERE policy_id=?", (policy_id,))
    if revision is None or pub is None:
        return None
    return _envelope(pub, revision, now, paths)


def list_publications(paths=None):
    paths = paths or config.PATHS
    with storage.session(paths) as connection:
        return _rows(connection, """SELECT v.country, v.title, p.policy_id, p.status, p.revision_id,
                r.revision_no, p.published_by, p.published_at, p.verified_at, p.withdraw_reason
            FROM POLICY_PUBLICATION p JOIN POLICY_REVISION r USING (revision_id)
            JOIN VISA_POLICY v USING (policy_id) ORDER BY v.country, v.title""")
