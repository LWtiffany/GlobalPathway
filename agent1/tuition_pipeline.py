"""Incremental capture of programme tuition into SCHOOL / PROGRAM / TUITION (tuition sources).

Same mechanics as the visa / FX pipelines: conditional probe -> Crawl4AI snapshot
-> per-section extraction cached in section_cache.json. A source declares its
school and either its programmes ("programs") or "catalog": true, in which case
programmes, divisions and school-wide rates are discovered from the page labels
(PROGRAM.scope). The scan unit is one school (all of its tuition pages). Each
verified fee row becomes one TUITION row keyed by (programme, academic_year,
fee_status, fee_basis, study_mode).

Write policy matches FX_POLICY: a new price point is inserted, an unchanged one
only refreshes its evidence, a changed amount or a cross-page conflict goes to
REVIEW_QUEUE (domain_table TUITION) and only `review approve` applies it. Rows
no longer on the page are kept (older academic years stay as history). Verbatim
quotes land in TUITION_EVIDENCE. Nothing is published.
"""
import json
import uuid

from . import config, crawl, formal_pipeline as fp, gate, review_text, storage, tuition_extract
from .models import LEVELS

DOMAIN = "tuition"


def _members(sources):
    return [s for s in sources.values() if s.get("schema_type") == DOMAIN and s.get("enabled")]


def school_keys(sources=None):
    sources = sources if sources is not None else fp.load_sources()
    return sorted({s["school"]["key"] for s in _members(sources)})


def _school_sources(key, sources):
    return [s for s in _members(sources) if s["school"]["key"] == key]


def _targets(name, sources):
    """A school key, a region (every school with a page there) or all."""
    if name == "all":
        return school_keys(sources)
    if name in school_keys(sources):
        return [name]
    schools = sorted({s["school"]["key"] for s in _members(sources) if s.get("region") == name})
    if not schools:
        raise ValueError(f"没有启用的学费来源属于学校或区域：{name}")
    return schools


def school_id(key):
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"school:{key}"))


def program_id(school_key, program_key):
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"program:{school_key}:{program_key}"))


def tuition_id(program_id_, academic_year, fee_status, fee_basis, study_mode, rate=None):
    parts = (program_id_, academic_year, fee_status, fee_basis, study_mode, *([rate] if rate else []))
    return str(uuid.uuid5(uuid.NAMESPACE_URL, "tuition:" + "|".join(str(p or "-") for p in parts)))


def validate_registry(source):
    school = source.get("school") or {}
    if not all(school.get(k) for k in ("key", "name", "country")):
        raise ValueError(f"学费来源 {source['id']} 缺少 school.key / name / country")
    unknown = set(source.get("defaults") or {}) - set(tuition_extract.DEFAULTABLE)
    if unknown:
        raise ValueError(f"学费来源 {source['id']} 的 defaults 含未知字段：{sorted(unknown)}")
    if tuition_extract.is_catalog(source):
        if source.get("programs"):
            raise ValueError(f"学费来源 {source['id']} 不能同时登记 catalog 与 programs")
        if source.get("level") not in (None, *LEVELS):
            raise ValueError(f"学费来源 {source['id']} 的 level 须为 {LEVELS} 之一")
        return
    programs = source.get("programs") or []
    keys = [p.get("key") for p in programs]
    if not programs or not all(keys) or len(set(keys)) != len(keys) \
            or tuition_extract.ALL_PROGRAMS in keys or not all(p.get("name") for p in programs):
        raise ValueError(f"学费来源 {source['id']} 的 programs 须为非空、key 唯一且带 name 的列表")


# ---------- domain writes ----------

def _upsert_program(connection, sid, school_key, key, name, level, scope):
    connection.execute("""INSERT INTO PROGRAM (program_id, school_id, name, level, scope)
        VALUES (%s,%s,%s,%s,%s) ON CONFLICT(program_id) DO UPDATE SET name=excluded.name,
        level=COALESCE(excluded.level, PROGRAM.level), scope=excluded.scope""",
        (program_id(school_key, key), sid, name, level, scope))


def _upsert_registry(connection, members, merged):
    """SCHOOL from the registry; PROGRAM from registered programmes and catalog rows."""
    school = members[0]["school"]
    sid = school_id(school["key"])
    connection.execute("""INSERT INTO SCHOOL (school_id, name, country, city) VALUES (%s,%s,%s,%s)
        ON CONFLICT(school_id) DO UPDATE SET name=excluded.name, country=excluded.country,
        city=excluded.city""", (sid, school["name"], school["country"], school.get("city")))
    for source in members:
        for program in source.get("programs") or []:
            _upsert_program(connection, sid, school["key"], program["key"], program["name"],
                            program.get("level"), None)
    for key, entry in merged.items():
        row = entry["support"][0]
        if row.program_scope:
            _upsert_program(connection, sid, school["key"], key[0], row.program_name, row.level,
                            row.program_scope)


def _replace_evidence(connection, tuition_id_, rows):
    items = []
    for row in rows:
        if not (row.get("source_id") and row.get("snapshot_id")):
            continue
        items.append(("amount", row["source_id"], row["snapshot_id"], row["evidence_quote"]))
        items += [("context", row["source_id"], row["snapshot_id"], q) for q in row.get("context_quotes") or []]
    items = list(dict.fromkeys(items))
    if not items:
        return
    connection.execute("DELETE FROM TUITION_EVIDENCE WHERE tuition_id=%s", (tuition_id_,))
    now = fp._now()
    for kind, source_id, snapshot_id, quote in items:
        evidence_id = str(uuid.uuid5(uuid.NAMESPACE_URL, "|".join(
            ("tuition_evidence", tuition_id_, kind, source_id, snapshot_id, quote))))
        connection.execute("""INSERT INTO TUITION_EVIDENCE
            (evidence_id, tuition_id, kind, source_id, snapshot_id, quote, recorded_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (evidence_id) DO NOTHING""",
            (evidence_id, tuition_id_, kind, source_id, snapshot_id, quote, now))


def _same_amount(existing, amount, currency):
    return (existing["amount"] is not None and float(existing["amount"]) == float(amount)
            and existing["currency"] == currency)


def _reject_stale_reviews(connection, tuition_id_):
    """Close pending proposals once the latest verified row needs no review.

    A fee can change and create an UPDATE review, then return to the stored value
    before a reviewer acts.  Leaving that proposal OPEN would allow an obsolete
    amount to be applied after the later scan has already verified the current
    value.
    """
    connection.execute("""DELETE FROM admin.review_queue
        WHERE domain_table='TUITION' AND record_key=%s""",
                       (tuition_id_,))


def _remark(support, sources):
    notes = []
    if all(row.amount_kind == "tuition_and_fees" for row in support):
        notes.append("金额为页面给出的学费与杂费合计（tuition and fees）")
    for row in support:
        source = sources.get(row.source_id) or {}
        if source.get("defaults") and source.get("defaults_note"):
            notes.append(f"来源登记约定（{', '.join(sorted(source['defaults']))}）：{source['defaults_note']}")
    return "；".join(dict.fromkeys(notes)) or None


def _apply_school(connection, school_key, merged, sources):
    results = []
    for key, entry in merged.items():
        program_key, academic_year, fee_status, fee_basis, study_mode, rate = key
        pid = program_id(school_key, program_key)
        tid = tuition_id(pid, academic_year, fee_status, fee_basis, study_mode, rate)
        rate_label = entry["support"][0].rate_label
        row = {"tuition_id": tid, "program_id": pid, "academic_year": academic_year,
               "fee_status": fee_status, "fee_basis": fee_basis, "study_mode": study_mode,
               "rate_label": rate_label, "amount": entry["amount"], "currency": entry["currency"],
               "remark": _remark(entry["support"], sources)}
        support = [r.model_dump() for r in entry["support"]]
        result = {"program": entry["support"][0].program_name or program_key,
                  "academic_year": academic_year, "fee_status": fee_status, "fee_basis": fee_basis,
                  "study_mode": study_mode, "rate": rate_label, "amount": entry["amount"],
                  "currency": entry["currency"], "tuition_id": tid}
        existing = fp._row(connection, "SELECT * FROM TUITION WHERE tuition_id=%s", (tid,))
        payload = {"tuition": row, "program_key": program_key, "school_key": school_key}
        if entry["conflict"]:
            values = sorted({f"{r.amount:g} {r.currency}" for r in entry["all_support"]})
            result.update(result="review", review_id=fp._enqueue_review(
                connection, table="TUITION", key=tid, action="CREATE" if existing is None else "UPDATE",
                payload=payload, evidence=[r.model_dump() for r in entry["all_support"]],
                reason=review_text.tuition_conflict_sentence(values)))
        elif existing is None:
            columns = list(row)
            connection.execute(f"""INSERT INTO TUITION ({', '.join(columns)})
                VALUES ({', '.join('%s' for _ in columns)})""", [row[c] for c in columns])
            _replace_evidence(connection, tid, support)
            _reject_stale_reviews(connection, tid)
            result["result"] = "inserted"
        elif _same_amount(existing, entry["amount"], entry["currency"]):
            _replace_evidence(connection, tid, support)
            _reject_stale_reviews(connection, tid)
            result["result"] = "no_change"
        elif existing["amount"] is None:
            connection.execute("UPDATE TUITION SET amount=%s, currency=%s, remark=%s WHERE tuition_id=%s",
                               (row["amount"], row["currency"], row["remark"], tid))
            _replace_evidence(connection, tid, support)
            _reject_stale_reviews(connection, tid)
            result["result"] = "updated"
        else:
            result.update(result="review", review_id=fp._enqueue_review(
                connection, table="TUITION", key=tid, action="UPDATE", payload=payload,
                evidence=support,
                reason=f"金额变更：{existing['amount']:g} {existing['currency']} → "
                       f"{entry['amount']:g} {entry['currency']}"),
                before={"amount": existing["amount"], "currency": existing["currency"]})
        results.append(result)
    return results


def apply_review(connection, row, *, decided_by, note=None,
                 selected_amount=None, selected_currency=None):
    """Approve one OPEN TUITION review row (dispatched from formal_pipeline.apply_review)."""
    payload = json.loads(row["proposed_value"])
    tuition = dict(payload["tuition"])
    evidence = json.loads(row["evidence_json"] or "[]")
    candidates = {(float(item["amount"]), item["currency"]): item["amount"]
                  for item in evidence if item.get("amount") is not None and item.get("currency")}
    if len(candidates) > 1:
        if selected_amount is None or not selected_currency:
            choices = "、".join(f"{amount:g} {currency}" for amount, currency in sorted(candidates))
            raise ValueError(f"该学费审核含冲突金额（{choices}）；批准时必须明确选择 amount 和 currency")
        choice = (float(selected_amount), selected_currency.strip().upper())
        if choice not in candidates:
            raise ValueError("选择的金额和币种不在该审核项的原文证据候选中")
        tuition["amount"] = candidates[choice]
        tuition["currency"] = choice[1]
    elif selected_amount is not None or selected_currency:
        if selected_amount is None or not selected_currency:
            raise ValueError("amount 和 currency 必须同时提供")
        choice = (float(selected_amount), selected_currency.strip().upper())
        if choice not in candidates:
            raise ValueError("选择的金额和币种不在该审核项的原文证据候选中")
        tuition["amount"] = candidates[choice]
        tuition["currency"] = choice[1]
    tid = row["record_key"]
    if fp._row(connection, "SELECT program_id FROM PROGRAM WHERE program_id=%s", (tuition["program_id"],)) is None:
        raise ValueError(f"项目不存在：{tuition['program_id']}；请先重新扫描该学校")
    existing = fp._row(connection, "SELECT tuition_id FROM TUITION WHERE tuition_id=%s", (tid,))
    if row["action"] == "CREATE":
        if existing:
            raise ValueError(f"目标行已存在：{tid}；请用 UPDATE 类审核处理")
        columns = list(tuition)
        connection.execute(f"""INSERT INTO TUITION ({', '.join(columns)})
            VALUES ({', '.join('%s' for _ in columns)})""", [tuition[c] for c in columns])
    else:
        if not existing:
            raise ValueError(f"目标行不存在：{tid}")
        connection.execute("UPDATE TUITION SET amount=%s, currency=%s, remark=%s WHERE tuition_id=%s",
                           (tuition["amount"], tuition["currency"], tuition.get("remark"), tid))
    support = [r for r in evidence
               if float(r.get("amount") or 0) == float(tuition["amount"])
               and r.get("currency") == tuition["currency"]]
    _replace_evidence(connection, tid, support)
    connection.execute("DELETE FROM admin.review_queue WHERE review_id=%s", (row["review_id"],))
    return {"review_id": row["review_id"], "action": row["action"], "tuition_id": tid,
            "applied_fields": ["amount", "currency"]}


# ---------- scan orchestration ----------

def _scan_school(key, *, paths, fresh_ids, full, sources, reapply=False):
    members = _school_sources(key, sources)
    if not members:
        raise ValueError(f"{key} 没有启用的学费来源")
    for source in members:
        validate_registry(source)
    settings = config.llm_settings()
    client_box = [None, settings]
    captures, pages = [], []
    for source in members:
        fresh = source["id"] in fresh_ids
        try:
            capture = fp._capture(source, paths, settings, client_box, fresh=fresh,
                                  full=full and fresh, extractor=tuition_extract)
        except Exception as exc:
            if len(fresh_ids) == 1 and fresh:
                raise
            pages.append({"source_id": source["id"], "status": "FAILED",
                          "error": f"{type(exc).__name__}: {exc}"})
            try:
                capture = fp._capture(source, paths, settings, client_box, fresh=False,
                                      full=False, extractor=tuition_extract)
            except Exception:
                capture = None
            if capture is None:
                continue
        if capture is None:
            pages.append({"source_id": source["id"], "status": "MISSING",
                          "error": "尚无快照；用 scan-tuition 或 scan 该来源先抓取一次"})
            continue
        captures.append(capture)

    totals = {"model_calls": sum(c["model_calls"] for c in captures),
              "prompt_tokens": sum(c["tokens"][0] for c in captures),
              "completion_tokens": sum(c["tokens"][1] for c in captures),
              "reused_sections": sum(c["reused_sections"] for c in captures)}
    changed = [c for c in captures if c["fresh"] and c["status"] not in fp.UNCHANGED]
    if not changed and not reapply:
        for capture in captures:
            if "state" in capture:
                fp._save_state(paths, capture["source"]["id"], capture["state"])
        return {"result": "no_change", "school": key, "applied": False, **totals,
                "pages": pages + [{"source_id": c["source"]["id"], "status": c["status"]}
                                  for c in captures]}

    verified, reports = [], []
    for capture in captures:
        source, snapshot_id = capture["source"], capture["snapshot_id"]
        report = gate.GateReport()
        rows = 0
        labels = tuition_extract.page_labels(capture["patches"])
        for section in capture["patches"]:
            clean, _ = tuition_extract.verify(section, capture["markdown"], source, snapshot_id, report,
                                              labels)
            verified += clean.rows
            rows += len(clean.rows)
        # rejected rows are dropped before the merge; they are logged, not queued
        page_report = gate.GateReport()
        for problem in report.problems:
            fields = {k: problem[k] for k in ("field", "value", "quote", "scope") if k in problem}
            reason = (f"已丢弃无原文支撑的学费行：{problem['reason']}" if problem["severity"] == "error"
                      else problem["reason"])
            page_report.problem(problem["check"], "warning", reason, problem.get("suggested_action"), **fields)
        reports.append((capture, page_report))
        pages.append({"source_id": source["id"], "status": capture["status"], "snapshot_id": snapshot_id,
                      "model_calls": capture["model_calls"], "reused_sections": capture["reused_sections"],
                      "rows": rows, "warnings": [p["reason"] for p in page_report.problems]})

    keys_by_source = {s["id"]: [p["key"] for p in s.get("programs") or []] for s in members}
    merged, conflicts = tuition_extract.merge(verified, keys_by_source)
    if not merged:
        raise ValueError(f"{key} 所有学费页面都没有抽取到可核实的学费；保留已入库数据并检查覆盖范围")
    school_report = gate.GateReport()
    for conflict in conflicts:
        program_key, year, status, basis, mode, rate = conflict["key"]
        school_report.problem("conflict", "error",
                              f"跨页面：{program_key} {year} {status} {basis} {mode} {rate or ''} "
                              f"金额冲突 {conflict['values']}",
                              "人工核对各页面原文，在审核中选定金额", field=f"tuition:{program_key}",
                              value=fp._json(conflict["values"]), scope=program_key)
    with storage.session(paths, readonly=False) as connection:
        _upsert_registry(connection, members, merged)
        applied = _apply_school(connection, key, merged, sources)
        issue_ids = []
        for capture, report in reports:
            issue_ids += fp._persist_gate_issues(connection, capture["source"], capture["snapshot_id"], report)
        issue_ids += fp._persist_gate_issues(connection, {"id": f"school:{key}"}, None, school_report)
    for capture in captures:
        if "state" in capture:
            fp._save_state(paths, capture["source"]["id"], capture["state"])
    problems = [p for _, r in reports for p in r.problems] + school_report.problems
    return {"result": "applied", "school": key, "applied": True, "pages": pages, **totals,
            "tuition": applied, "gate_issue_ids": issue_ids,
            "gate": {"errors": [p["reason"] for p in problems if p["severity"] == "error"],
                     "warnings": [p["reason"] for p in problems if p["severity"] == "warning"]}}


def scan(source_id, *, paths=None, full=False):
    """Re-check one tuition source; the school's other pages reuse their last snapshot."""
    paths = paths or config.PATHS
    fp._guard(paths)
    sources = fp.load_sources()
    source = sources[source_id]
    crawl.validate_source(source)
    validate_registry(source)
    outcome = _scan_school(source["school"]["key"], paths=paths, fresh_ids={source_id},
                           full=full, sources=sources)
    outcome["source_id"] = source_id
    return outcome


def scan_all(target="all", *, paths=None, full=False, reapply=False):
    """Re-check every tuition page of a school, a region or all; reapply re-merges stored snapshots."""
    paths = paths or config.PATHS
    fp._guard(paths)
    sources = fp.load_sources()
    keys = _targets(target, sources)
    outcomes = []
    for key in keys:
        fresh = set() if reapply else {s["id"] for s in _school_sources(key, sources)}
        try:
            outcomes.append(_scan_school(key, paths=paths, fresh_ids=fresh, full=full and not reapply,
                                         sources=sources, reapply=reapply))
        except Exception as exc:  # noqa: BLE001 - one school must not stop a batch
            if len(keys) == 1:
                raise
            outcomes.append({"result": "failed", "school": key, "applied": False, "error": str(exc)})
    return outcomes[0] if len(outcomes) == 1 and target != "all" else {"result": "applied", "schools": outcomes}
