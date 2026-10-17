"""Incremental capture of study-abroad FX rules into FX_POLICY (fx_policy sources).

Same mechanics as the visa pipeline (formal_pipeline): conditional probe ->
Crawl4AI snapshot -> per-section extraction cached in section_cache.json. Each
section is verified against its page snapshot (fx_extract.verify), then the
verified facts of every fx_policy page of a country are merged into one
FX_POLICY row per study purpose (tuition / living). Facts without a purpose apply
to both rows.

Write policy matches VISA_POLICY: empty columns are filled, a changed value or a
cross-page conflict goes to REVIEW_QUEUE (domain_table FX_POLICY) and only a
human `review approve` applies it. Documents land in POLICY_DOCUMENT with
policy_domain 'fx_policy'; verbatim quotes in FX_EVIDENCE. Nothing is published.
"""
import json
import uuid

from . import config, crawl, formal_pipeline as fp, fx_extract, gate, review_text, storage
from .models import FX_FIELDS, FX_PURPOSES

DOMAIN = "fx_policy"
POLICY_TYPE = "study_abroad"
TITLES = {"tuition": "留学购汇：学费", "living": "留学购汇：生活费"}


def fx_regions(sources=None):
    sources = sources if sources is not None else fp.load_sources()
    return sorted({s.get("region") or "unknown" for s in sources.values()
                   if s.get("schema_type") == DOMAIN and s.get("enabled")})


def _region_sources(region, sources):
    return [s for s in sources.values()
            if s.get("schema_type") == DOMAIN and s.get("enabled")
            and (s.get("region") or "unknown") == region]


def policy_id(country, purpose):
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"fx_policy:{country}:{POLICY_TYPE}:{purpose}"))


# ---------- domain writes ----------

def _apply_documents(connection, policy_id_, documents):
    for document in documents or []:
        link = fp._document_link(document)
        connection.execute("INSERT INTO DOCUMENT (doc_id, code, name_zh) VALUES (%s,%s,%s) "
                           "ON CONFLICT (doc_id) DO NOTHING",
                           (link["doc_id"], link["code"], link["name_zh"]))
        connection.execute('INSERT INTO POLICY_DOCUMENT '
                           '(policy_id, policy_domain, doc_id, required, "condition") '
                           'VALUES (%s,%s,%s,%s,%s) ON CONFLICT (policy_id, policy_domain, doc_id) '
                           'DO UPDATE SET required=EXCLUDED.required, "condition"=EXCLUDED."condition"',
                           (policy_id_, DOMAIN, link["doc_id"], link["required"], link["condition"]))


def _current_link(connection, policy_id_, doc_id):
    return fp._row(connection, """SELECT required, "condition" FROM POLICY_DOCUMENT
        WHERE policy_id=%s AND policy_domain=%s AND doc_id=%s""", (policy_id_, DOMAIN, doc_id))


def _sync_documents(connection, policy_id_, documents):
    new, changed = [], []
    for document in documents:
        link = fp._document_link(document)
        existing = _current_link(connection, policy_id_, link["doc_id"])
        if existing is None:
            new.append(document)
        elif (existing["required"], existing["condition"]) != (link["required"], link["condition"]):
            changed.append(document)
    _apply_documents(connection, policy_id_, new)
    return new, changed


def _vanished_documents(connection, policy_id_, named, pages):
    rows = connection.execute("""SELECT d.doc_id, d.code, d.name_zh FROM POLICY_DOCUMENT pd
        JOIN DOCUMENT d USING (doc_id) WHERE pd.policy_id=%s AND pd.policy_domain=%s
        ORDER BY d.code, d.name_zh""", (policy_id_, DOMAIN))
    vanished = []
    for row in rows:
        if row["doc_id"] in named:
            continue
        quotes = connection.execute("""SELECT source_id, quote FROM FX_EVIDENCE
            WHERE policy_id=%s AND target=%s AND kind='value'""",
            (policy_id_, f"document:{row['doc_id']}")).fetchall()
        vanished.append({**dict(row), "still_quoted": any(
            fx_extract.quote_on_page(q["quote"], pages.get(q["source_id"], "")) for q in quotes)})
    return vanished


def _remove_documents(connection, policy_id_, documents):
    for document in documents or []:
        connection.execute("""DELETE FROM POLICY_DOCUMENT
            WHERE policy_id=%s AND policy_domain=%s AND doc_id=%s""",
                           (policy_id_, DOMAIN, document["doc_id"]))
        connection.execute("DELETE FROM FX_EVIDENCE WHERE policy_id=%s AND target=%s",
                           (policy_id_, f"document:{document['doc_id']}"))


def _replace_evidence(connection, policy_id_, target, items):
    """items: (kind, source_id, snapshot_id, quote); untraceable quotes are not stored."""
    items = list(dict.fromkeys(i for i in items if all(i)))
    if not items:
        return
    connection.execute("DELETE FROM FX_EVIDENCE WHERE policy_id=%s AND target=%s", (policy_id_, target))
    now = fp._now()
    for kind, source_id, snapshot_id, quote in items:
        evidence_id = str(uuid.uuid5(uuid.NAMESPACE_URL, "|".join(
            ("fx_evidence", policy_id_, target, kind, source_id, snapshot_id, quote))))
        connection.execute("""INSERT INTO FX_EVIDENCE
            (evidence_id, policy_id, target, kind, source_id, snapshot_id, quote, recorded_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (evidence_id) DO NOTHING""",
            (evidence_id, policy_id_, target, kind, source_id, snapshot_id, quote, now))


def _record_evidence(connection, policy_id_, evidence, documents):
    """Refresh quotes for the values the row holds now (same value as this run's support)."""
    row = fp._row(connection, "SELECT * FROM FX_POLICY WHERE policy_id=%s", (policy_id_,))
    if row is None:
        return
    for field, facts in evidence.items():
        values = {fact["value"] for fact in facts}
        if row.get(field) is None or row[field] not in values:
            continue
        _replace_evidence(connection, policy_id_, f"field:{field}", [
            ("value", f.get("source_id"), f.get("snapshot_id"), f["evidence_quote"])
            for f in facts if f["value"] == row[field]])
    for document in documents or []:
        link = fp._document_link(document)
        current = _current_link(connection, policy_id_, link["doc_id"])
        if current is None or (current["required"], current["condition"]) != (link["required"], link["condition"]):
            continue
        items = []
        for support in document.get("support") or []:
            items.append(("value", support["source_id"], support["snapshot_id"], support["evidence_quote"]))
            for part in (support.get("condition_quote") or "").split(gate.CONDITION_QUOTE_SEPARATOR):
                if part.strip() and link["condition"]:
                    items.append(("condition", support["source_id"], support["snapshot_id"], part))
        _replace_evidence(connection, policy_id_, f"document:{link['doc_id']}", items)


def _evidence_json(evidence):
    return {field: [f.model_dump() for f in facts] for field, facts in evidence.items()}


def _flat_evidence(evidence):
    return [fact for facts in evidence.values() for fact in facts]


def _reject_stale_reviews(connection, policy_id_):
    """Close pending proposals when the latest verified state needs no review."""
    connection.execute("""DELETE FROM admin.review_queue
        WHERE domain_table='FX_POLICY' AND record_key=%s""",
                       (policy_id_,))


def _apply_country(connection, country, merged, conflicts, named=None, pages=None):
    """Write the merged per-purpose rules of one country; returns one result per purpose."""
    results = []
    for purpose in FX_PURPOSES:
        bucket = merged[purpose]
        documents = [d.model_dump(exclude_none=True) for d in bucket["documents"]]
        evidence = _evidence_json(bucket["evidence"])
        clean = {f: v for f, v in bucket["fields"].items() if f not in bucket["conflicted"]}
        blocked = {f: v for f, v in bucket["fields"].items() if f in bucket["conflicted"]}
        reasons = [review_text.conflict_sentence(c["field"], c["values"]) for c in conflicts
                   if c["field"] in blocked and c["scope"] in (None, purpose)]
        id_ = policy_id(country, purpose)
        header = {"policy_id": id_, "country": country, "policy_type": POLICY_TYPE,
                  "title": TITLES[purpose], "purpose_code": purpose}
        existing = fp._row(connection, "SELECT * FROM FX_POLICY WHERE policy_id=%s", (id_,))
        has_currency = "annual_quota_currency" in clean or (
            existing is not None and existing["annual_quota_currency"] is not None)
        if "annual_quota_amount" in clean and not has_currency:
            blocked["annual_quota_amount"] = clean.pop("annual_quota_amount")
            reasons.append(review_text.missing_currency_sentence())
        result = {"purpose": purpose, "policy_id": id_}

        if existing is None:
            if clean or documents:
                columns = ["policy_id", "country", "policy_type", "title", "purpose_code", *clean]
                values = [id_, country, POLICY_TYPE, TITLES[purpose], purpose, *clean.values()]
                connection.execute(f"""INSERT INTO FX_POLICY ({', '.join(columns)})
                    VALUES ({', '.join('%s' for _ in columns)})""", values)
                _apply_documents(connection, id_, documents)
                _record_evidence(connection, id_, evidence, documents)
                result.update(result="inserted", fields=sorted(clean), documents=len(documents))
                if blocked:
                    result["review_id"] = fp._enqueue_review(
                        connection, table="FX_POLICY", key=id_, action="UPDATE",
                        payload={"policy": {**header, **blocked}, "documents": []},
                        evidence=_flat_evidence(evidence), reason="；".join(reasons))
                else:
                    _reject_stale_reviews(connection, id_)
            elif blocked:
                result.update(result="review", review_id=fp._enqueue_review(
                    connection, table="FX_POLICY", key=id_, action="CREATE",
                    payload={"policy": {**header, **blocked}, "documents": documents},
                    evidence=_flat_evidence(evidence), reason="；".join(reasons)))
            else:
                result["result"] = "empty"
                _reject_stale_reviews(connection, id_)
            results.append(result)
            continue

        fill = {c: v for c, v in clean.items() if existing[c] is None}
        changed = {c: (existing[c], v) for c, v in clean.items()
                   if existing[c] is not None and existing[c] != v}
        if fill:
            connection.execute(f"""UPDATE FX_POLICY SET {', '.join(f'{c}=%s' for c in fill)}
                WHERE policy_id=%s""", [*fill.values(), id_])
        vanished = _vanished_documents(connection, id_, named, pages or {}) if named is not None else []
        new_docs, changed_docs = _sync_documents(connection, id_, documents)
        _record_evidence(connection, id_, evidence, documents)
        result.update(filled=sorted(fill), linked_documents=len(new_docs))
        review_row = {**{c: after for c, (_, after) in changed.items()},
                      **{c: v for c, v in blocked.items() if existing[c] != v}}
        if review_row or changed_docs or vanished:
            parts = list(reasons)
            if changed:
                parts.append(review_text.change_sentence(changed))
            if changed_docs:
                parts.append("材料要求变更：" + "、".join(d["name_zh"] for d in changed_docs))
            gone = [d["name_zh"] for d in vanished if not d["still_quoted"]]
            missed = [d["name_zh"] for d in vanished if d["still_quoted"]]
            if gone:
                parts.append("页面不再列出的材料（批准后移除）：" + "、".join(gone))
            if missed:
                parts.append("本次未抽到、但原引用仍在页面上的材料（批准后同样移除，请先核对是否漏抽）："
                             + "、".join(missed))
            result.update(result="review", review_id=fp._enqueue_review(
                connection, table="FX_POLICY", key=id_, action="UPDATE",
                payload={"policy": {**header, **review_row}, "documents": changed_docs,
                         "removed_documents": vanished},
                evidence=_flat_evidence(evidence), reason="；".join(parts)),
                changed={c: {"before": b, "after": a} for c, (b, a) in changed.items()})
        else:
            result["result"] = "updated" if fill or new_docs else "no_change"
            _reject_stale_reviews(connection, id_)
        results.append(result)
    _collapse_shared_reviews(connection, results)
    return results


def _collapse_shared_reviews(connection, results):
    """A rule with no purpose is copied onto tuition and living. Keep one review."""
    rows = []
    for result in results:
        review_id = result.get("review_id")
        if not review_id:
            continue
        row = fp._row(connection, "SELECT * FROM admin.review_queue WHERE review_id=%s", (review_id,))
        if row:
            rows.append(row)
    if len(rows) < 2:
        return

    def signature(row):
        payload = json.loads(row["proposed_value"] or "{}")
        policy = payload.get("policy") or {}
        fields = {name: policy.get(name) for name in FX_FIELDS if name in policy}
        choices = {}
        for fact in json.loads(row["evidence_json"] or "[]"):
            if fact.get("field") in fields:
                choices.setdefault(fact["field"], set()).add(
                    json.dumps(fact.get("value"), ensure_ascii=False, sort_keys=True))
        return json.dumps({
            "fields": fields,
            "choices": {name: sorted(values) for name, values in choices.items()},
            "documents": payload.get("documents") or [],
            "removed": payload.get("removed_documents") or [],
        }, ensure_ascii=False, sort_keys=True)

    grouped = {}
    for row in rows:
        grouped.setdefault(signature(row), []).append(row)
    for group in grouped.values():
        if len(group) < 2:
            continue
        keep, *rest = group
        payload = json.loads(keep["proposed_value"] or "{}")
        titles = []
        for row in group:
            title = (json.loads(row["proposed_value"] or "{}").get("policy") or {}).get("title") or ""
            label = title.split("：", 1)[-1]
            if label and label not in titles:
                titles.append(label)
        payload["also_apply"] = [row["record_key"] for row in rest]
        payload["shared_titles"] = titles
        connection.execute("UPDATE admin.review_queue SET proposed_value=%s WHERE review_id=%s",
                           (json.dumps(payload, ensure_ascii=False), keep["review_id"]))
        for row in rest:
            connection.execute("DELETE FROM admin.review_queue WHERE review_id=%s", (row["review_id"],))


def apply_review(connection, row, *, decided_by, note=None, selected_fields=None):
    """Approve one OPEN FX_POLICY review row (dispatched from formal_pipeline.apply_review)."""
    payload = json.loads(row["proposed_value"])
    policy = dict(payload["policy"])
    id_ = row["record_key"]
    columns = [c for c in policy if c in FX_FIELDS]
    evidence_rows = json.loads(row["evidence_json"] or "[]")
    candidates = {}
    for fact in evidence_rows:
        field = fact.get("field")
        if field not in columns:
            continue
        value = fact.get("value")
        candidates.setdefault(field, {})[json.dumps(value, sort_keys=True, ensure_ascii=False)] = value
    selected_fields = selected_fields or {}
    unknown = set(selected_fields) - set(columns)
    if unknown:
        raise ValueError(f"选择了不属于该审核项的字段：{', '.join(sorted(unknown))}")
    for field in columns:
        choices = candidates.get(field, {})
        if len(choices) > 1 and field not in selected_fields:
            labels = "、".join(choices)
            raise ValueError(f"外汇字段 {field} 含冲突候选（{labels}）；批准时必须明确选择")
        if field in selected_fields:
            key = json.dumps(selected_fields[field], sort_keys=True, ensure_ascii=False)
            if key not in choices:
                raise ValueError(f"字段 {field} 选择的值不在该审核项的原文证据候选中")
            policy[field] = choices[key]
    if row["action"] == "CREATE":
        if fp._row(connection, "SELECT policy_id FROM FX_POLICY WHERE policy_id=%s", (id_,)):
            raise ValueError(f"目标行已存在：{id_}；请用 UPDATE 类审核处理")
        insert = ["policy_id", "country", "policy_type", "title", "purpose_code", *columns]
        connection.execute(f"""INSERT INTO FX_POLICY ({', '.join(insert)})
            VALUES ({', '.join('%s' for _ in insert)})""",
            [id_, policy.get("country", "unknown"), policy.get("policy_type", POLICY_TYPE),
             policy.get("title", id_), policy.get("purpose_code")] + [policy[c] for c in columns])
    elif columns:
        connection.execute(f"""UPDATE FX_POLICY SET {', '.join(f'{c}=%s' for c in columns)}
            WHERE policy_id=%s""", [policy[c] for c in columns] + [id_])
    _apply_documents(connection, id_, payload.get("documents"))
    _remove_documents(connection, id_, payload.get("removed_documents"))
    evidence = {}
    for fact in evidence_rows:
        if fact.get("field") in columns:
            evidence.setdefault(fact["field"], []).append(fact)
    _record_evidence(connection, id_, evidence, payload.get("documents"))
    connection.execute("DELETE FROM admin.review_queue WHERE review_id=%s", (row["review_id"],))
    return {"review_id": row["review_id"], "action": row["action"], "policy_id": id_,
            "applied_fields": columns}


# ---------- scan orchestration ----------

def _scan_country(country, *, paths, fresh_ids, full, sources, reapply=False):
    members = _region_sources(country, sources)
    if not members:
        raise ValueError(f"{country} 没有启用的 fx_policy 来源")
    settings = config.llm_settings()
    client_box = [None, settings]
    captures, pages = [], []
    for source in members:
        fresh = source["id"] in fresh_ids
        try:
            capture = fp._capture(source, paths, settings, client_box, fresh=fresh,
                                  full=full and fresh, extractor=fx_extract)
        except Exception as exc:
            if len(fresh_ids) == 1 and fresh:
                raise
            pages.append({"source_id": source["id"], "status": "FAILED",
                          "error": f"{type(exc).__name__}: {exc}"})
            try:
                capture = fp._capture(source, paths, settings, client_box, fresh=False,
                                      full=False, extractor=fx_extract)
            except Exception:
                capture = None
            if capture is None:
                continue
        if capture is None:
            pages.append({"source_id": source["id"], "status": "MISSING",
                          "error": "尚无快照；用 scan-fx 或 scan 该来源先抓取一次"})
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
        return {"result": "no_change", "country": country, "applied": False, **totals,
                "pages": pages + [{"source_id": c["source"]["id"], "status": c["status"]}
                                  for c in captures]}

    verified, reports = [], []
    for capture in captures:
        source_id, snapshot_id = capture["source"]["id"], capture["snapshot_id"]
        report = gate.GateReport()
        for section in capture["patches"]:
            clean, _ = fx_extract.verify(section, capture["markdown"], source_id, snapshot_id, report)
            verified.append(clean)
        # rejected section values are dropped before the merge; they are logged, not queued
        page_report = gate.GateReport()
        for problem in report.problems:
            fields = {k: problem[k] for k in ("field", "value", "quote", "scope") if k in problem}
            if problem["severity"] == "error":
                page_report.problem(problem["check"], "warning",
                                    f"已丢弃无原文支撑的章节取值：{problem['reason']}",
                                    problem.get("suggested_action"), **fields)
            else:
                page_report.problem(problem["check"], "warning", problem["reason"],
                                    problem.get("suggested_action"), **fields)
        reports.append((capture, page_report))
        pages.append({"source_id": source_id, "status": capture["status"], "snapshot_id": snapshot_id,
                      "model_calls": capture["model_calls"],
                      "reused_sections": capture["reused_sections"],
                      "facts": sum(len(p.facts) for p in capture["patches"]),
                      "documents": sum(len(p.documents) for p in capture["patches"]),
                      "warnings": [p["reason"] for p in page_report.problems]})

    merged, conflicts, notes = fx_extract.merge(verified)
    if not any(merged[p]["fields"] or merged[p]["documents"] for p in FX_PURPOSES):
        raise ValueError(f"{country} 所有外汇页面都没有抽取到可核实的字段；保留已入库知识并检查覆盖范围")
    country_report = gate.GateReport()
    for conflict in conflicts:
        country_report.problem("conflict", "error", f"跨页面：{conflict['reason']}",
                               "人工核对各页面原文，在审核中选定取值", field=conflict["field"],
                               value=fp._json(conflict["values"]), scope=conflict["scope"])
    for note in notes:
        country_report.problem("conflict", "warning", note, "确认用途规则是否应覆盖通用规则")

    named = {fp._document_link(d.model_dump())["doc_id"]
             for c in captures for p in c["patches"] for d in p.documents if d.code.strip()} \
        if len(captures) == len(members) else None
    page_text = {c["source"]["id"]: c["markdown"] for c in captures}
    with storage.session(paths, readonly=False) as connection:
        applied = _apply_country(connection, country, merged, conflicts, named, page_text)
        issue_ids = []
        for capture, report in reports:
            issue_ids += fp._persist_gate_issues(connection, capture["source"],
                                                 capture["snapshot_id"], report)
        issue_ids += fp._persist_gate_issues(connection, {"id": f"region:fx:{country}"}, None,
                                             country_report)
    for capture in captures:
        if "state" in capture:
            fp._save_state(paths, capture["source"]["id"], capture["state"])
    problems = [p for _, r in reports for p in r.problems] + country_report.problems
    return {"result": "applied", "country": country, "applied": True, "pages": pages, **totals,
            "policies": applied, "gate_issue_ids": issue_ids,
            "gate": {"errors": [p["reason"] for p in problems if p["severity"] == "error"],
                     "warnings": [p["reason"] for p in problems if p["severity"] == "warning"]}}


def scan(source_id, *, paths=None, full=False):
    """Re-check one fx_policy source; its country's other pages reuse their last snapshot."""
    paths = paths or config.PATHS
    fp._guard(paths)
    sources = fp.load_sources()
    source = sources[source_id]
    crawl.validate_source(source)
    country = source.get("region") or "unknown"
    outcome = _scan_country(country, paths=paths, fresh_ids={source_id}, full=full, sources=sources)
    outcome["source_id"] = source_id
    return outcome


def scan_all(country="all", *, paths=None, full=False, reapply=False):
    """Re-check every fx_policy page of a country (or all); reapply re-merges stored snapshots."""
    paths = paths or config.PATHS
    fp._guard(paths)
    sources = fp.load_sources()
    countries = fx_regions(sources) if country == "all" else [country]
    outcomes = []
    for name in countries:
        fresh = set() if reapply else {s["id"] for s in _region_sources(name, sources)}
        outcomes.append(_scan_country(name, paths=paths, fresh_ids=fresh, full=full and not reapply,
                                      sources=sources, reapply=reapply))
    return outcomes[0] if country != "all" else {"result": "applied", "countries": outcomes}
