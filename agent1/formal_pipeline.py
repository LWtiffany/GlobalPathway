"""Incremental capture to evidence-backed candidates in the new database.

This service never publishes. A model result can only create candidates and issues.
"""
import asyncio
import json
import re
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

from . import candidate_compare, config, crawl, formal_extract, gate, incremental, storage
from .models import VisaPatch


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


def _check_event(connection, source_id, result, snapshot_id=None, **detail):
    connection.execute("INSERT INTO source_check_events VALUES(?,?,?,?,?,?)",
                       (_id("chk"), source_id, result, snapshot_id, _now(), _json(detail)))


def register_sources(path, paths=None):
    """Import explicit approvals into the isolated development/test formal registry."""
    paths = paths or config.PATHS
    if paths.environment == "production":
        raise ValueError("正式来源审批尚未接入；不能把 sources.json 的开发标记当正式审批")
    entries = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(entries, list):
        raise ValueError("来源配置必须为 JSON 数组")
    count = 0
    with storage.session(paths, readonly=False) as connection:
        for entry in entries:
            source_id = entry["id"]
            if not re.fullmatch(r"[a-z][a-z0-9_-]{0,79}", source_id):
                raise ValueError(f"来源 ID 不安全：{source_id}")
            parsed = urlparse(entry["url"])
            if parsed.scheme != "https" or parsed.hostname != entry["allowed_host"]:
                raise ValueError(f"来源 {source_id} URL 与登记主机不匹配")
            crawl_config = {"content_selector": entry.get("content_selector"),
                            "dynamic": bool(entry.get("dynamic", False)),
                            "schema_type": entry.get("schema_type", "generic_facts"),
                            "full_recheck_days": 7}
            now = _now()
            connection.execute("""INSERT INTO sources (
                source_id,name,url,allowed_host,category,country,authority,language,owner,
                access_basis,review_status,enabled,check_interval_minutes,freshness_sla_hours,
                crawl_config_json,created_at,updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(source_id) DO UPDATE SET name=excluded.name,url=excluded.url,
                allowed_host=excluded.allowed_host,category=excluded.category,country=excluded.country,
                authority=excluded.authority,language=excluded.language,owner=excluded.owner,
                access_basis=excluded.access_basis,check_interval_minutes=excluded.check_interval_minutes,
                freshness_sla_hours=excluded.freshness_sla_hours,crawl_config_json=excluded.crawl_config_json,
                updated_at=excluded.updated_at""",
                (source_id, entry.get("name", source_id), entry["url"], entry["allowed_host"],
                 entry.get("category", "school"), entry.get("region"), entry.get("authority", "official"),
                 entry.get("language", "en"), entry.get("owner", ""), entry.get("access_basis", ""),
                 entry.get("review_status", "pending"), int(bool(entry.get("enabled", False))),
                 int(entry.get("check_interval_minutes", 1440)),
                 int(entry.get("freshness_sla_hours", 168)), _json(crawl_config), now, now))
            count += 1
    return count


def _source(connection, source_id):
    row = _row(connection, "SELECT * FROM sources WHERE source_id=?", (source_id,))
    if not row:
        raise ValueError(f"新库未登记来源：{source_id}；先运行 db sync-sources")
    config_json = json.loads(row["crawl_config_json"])
    row.update(config_json)
    row["schema_type"] = config_json.get("schema_type", "generic_facts")
    return row


def _current_snapshot(connection, state):
    if not state or not state.get("snapshot_id"):
        return None
    return _row(connection, "SELECT * FROM source_snapshots WHERE snapshot_id=?", (state["snapshot_id"],))


def _finish_run(connection, run_id, status, snapshot_id=None, error_kind=None, error_message=None):
    connection.execute("""UPDATE crawl_runs SET status=?,snapshot_id=?,finished_at=?,
        error_kind=?,error_message=? WHERE run_id=?""",
        (status, snapshot_id, _now(), error_kind, error_message, run_id))


def _next_due(source, now):
    return (now + timedelta(minutes=source["check_interval_minutes"])).isoformat()


def _record_unchanged(paths, source, run_id, state, snapshot, result, probe, reason):
    checked = _now()
    with storage.session(paths, readonly=False) as connection:
        _finish_run(connection, run_id, "SUCCEEDED", snapshot["snapshot_id"])
        connection.execute("""INSERT INTO source_check_state
            (source_id,etag,last_modified,raw_body_hash,cache_epoch,last_extraction_profile,
             snapshot_id,last_checked_at,last_complete_at,last_full_at,next_check_at,last_result)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(source_id) DO UPDATE SET etag=excluded.etag,
            last_modified=excluded.last_modified,raw_body_hash=excluded.raw_body_hash,
            snapshot_id=excluded.snapshot_id,last_checked_at=excluded.last_checked_at,
            next_check_at=excluded.next_check_at,last_result=excluded.last_result""",
            (source["source_id"], probe.etag if probe else state.get("etag"),
             probe.last_modified if probe else state.get("last_modified"),
             probe.raw_body_hash if probe else state.get("raw_body_hash"),
             state.get("cache_epoch", "initial"), state.get("last_extraction_profile"),
             snapshot["snapshot_id"], checked, state.get("last_complete_at"),
             state.get("last_full_at"), _next_due(source, datetime.now(timezone.utc)), result))
        _check_event(connection, source["source_id"], result, snapshot["snapshot_id"], reason=reason)
    return {"result": result.lower(), "source_id": source["source_id"],
            "snapshot_id": snapshot["snapshot_id"], "model_calls": 0}


def _load_baseline(connection, policy_id):
    pointer = _row(connection, "SELECT revision_id FROM policy_publication WHERE policy_id=? AND status='ACTIVE'",
                   (policy_id,))
    if not pointer:
        return None, [], None
    revision = _row(connection, "SELECT * FROM visa_policy_revision WHERE revision_id=?", (pointer["revision_id"],))
    policy = {key: revision[key] for key in (
        "effective_from", "effective_to", "cas_required", "i20_required", "funds_tuition_required",
        "funds_tuition_unpaid", "funds_tuition_currency", "funds_living_monthly",
        "funds_living_currency", "funds_living_months", "funds_holding_days",
        "funds_doc_max_age_days", "funds_owner_allowed", "english_level", "tb_test_required",
        "health_surcharge", "apply_window_days", "processing_days", "visa_fee", "applies_to_city", "remark")
        if revision[key] is not None}
    policy["conditions"] = json.loads(revision["conditions_json"])
    documents = [dict(row) for row in connection.execute("""SELECT d.code,p.required,p.condition_json
        FROM policy_document p JOIN document d ON d.doc_id=p.doc_id WHERE p.revision_id=?""",
        (revision["revision_id"],))]
    for document in documents:
        document["condition"] = json.loads(document["condition_json"]) if document["condition_json"] else None
    return policy, documents, revision["revision_id"]


def _save_candidates(connection, source, patch, extraction_id, report):
    results = []
    for entry in patch.policies:
        proposed = entry.policy.model_dump(exclude_none=True)
        scope = (proposed.get("applies_to_city") or "all").strip().casefold().replace(" ", "_")
        policy_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{source['source_id']}:visa:student:money:{scope}"))
        connection.execute("""INSERT OR IGNORE INTO visa_policy
            (policy_id,source_id,country,policy_type,policy_topic,scope_code,title,created_at)
            VALUES(?,?,?,?,?,?,?,?)""",
            (policy_id, source["source_id"], source["country"] or "unknown", "student",
             "money", scope, f"Student visa money: {scope}", _now()))
        baseline, base_docs, revision_id = _load_baseline(connection, policy_id)
        docs = [doc.model_dump(exclude_none=True) for doc in patch.documents]
        comparison = candidate_compare.compare(baseline, base_docs, proposed, docs,
                                                complete=not report.errors)
        if comparison.result == "NO_CHANGE":
            results.append({"scope": scope, "result": "no_change"})
            continue
        payload = {"policy": proposed, "documents": docs}
        canonical = {"policy": candidate_compare.canonical_policy(proposed),
                     "documents": candidate_compare.canonical_documents(docs)}
        content_hash = incremental.fingerprint(_json(canonical))
        existing = _row(connection, """SELECT candidate_id FROM change_candidates
            WHERE policy_id=? AND normalized_hash=? AND normalization_version=?
              AND ((base_revision_id IS NULL AND ? IS NULL) OR base_revision_id=?)
              AND status IN ('DRAFT','PENDING_REVIEW') LIMIT 1""",
            (policy_id, content_hash, "business-1", revision_id, revision_id))
        if existing:
            results.append({"scope": scope, "result": "reused_candidate",
                            "candidate_id": existing["candidate_id"]})
            continue
        candidate_id = _id("cand")
        issues = list(comparison.issues) + [p["reason"] for p in report.problems]
        has_error = bool(report.errors or comparison.issues)
        gate_result = "ERROR" if has_error else "WARNING" if report.warnings else "PASS"
        status = "DRAFT" if has_error else "PENDING_REVIEW"
        connection.execute("""INSERT INTO change_candidates
            (candidate_id,source_id,extraction_id,domain,policy_id,base_revision_id,
             normalized_hash,normalization_version,proposed_payload_json,diff_json,evidence_json,
             gate_result,status,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (candidate_id, source["source_id"], extraction_id, "visa_policy", policy_id,
             revision_id, content_hash, "business-1", _json(payload),
             _json({"comparison": comparison.result, "changes": comparison.changes, "issues": issues}),
             _json([ev.model_dump() for ev in entry.evidence]), gate_result, status, _now()))
        for problem in report.problems:
            connection.execute("""INSERT INTO quality_issues
                (issue_id,source_id,candidate_id,field_path,check_name,severity,reason,status,created_at)
                VALUES(?,?,?,?,?,?,?,?,?)""",
                (_id("issue"), source["source_id"], candidate_id, problem.get("field"),
                 problem["check"], problem["severity"].upper(), problem["reason"], "OPEN", _now()))
        for issue in comparison.issues:
            connection.execute("""INSERT INTO quality_issues
                (issue_id,source_id,candidate_id,check_name,severity,reason,status,created_at)
                VALUES(?,?,?,?,?,?,?,?)""",
                (_id("issue"), source["source_id"], candidate_id, "comparison", "WARNING", issue,
                 "OPEN", _now()))
        if status in ("PENDING_REVIEW", "DRAFT"):
            connection.execute("""INSERT INTO review_tasks
                (review_id,candidate_id,reason,status,created_at) VALUES(?,?,?,?,?)""",
                (_id("review"), candidate_id,
                 "needs_resolution" if status == "DRAFT" else comparison.result, "OPEN", _now()))
        results.append({"scope": scope, "result": status.lower(), "candidate_id": candidate_id,
                        "issues": issues})
    return results


def scan(source_id, *, paths=None, full=False):
    """Process one registered source. Production remains blocked until publication is wired."""
    paths = paths or config.PATHS
    if paths.environment == "production":
        raise ValueError("正式环境采集须等待审核/发布服务接入")
    if not re.fullmatch(r"[a-z][a-z0-9_-]{0,79}", source_id):
        raise ValueError("来源 ID 不符合安全路径规则")
    with storage.session(paths) as connection:
        source = _source(connection, source_id)
        state = _row(connection, "SELECT * FROM source_check_state WHERE source_id=?", (source_id,)) or {}
        previous = _current_snapshot(connection, state)
    if source["schema_type"] != "visa_policy":
        raise ValueError("新管道目前只支持 visa_policy；其他领域待专用抽取器接入")
    crawl.validate_source(source)
    settings = config.llm_settings()
    epoch = _id("epoch") if full else state.get("cache_epoch", "initial")
    profile = formal_extract.extraction_profile(settings, epoch)
    now = datetime.now(timezone.utc)
    due = storage_timestamp(state.get("next_check_at"))
    last_full = storage_timestamp(state.get("last_full_at"))
    full_days = int(source.get("full_recheck_days", 7))
    if last_full is None or now - last_full >= timedelta(days=full_days):
        full = True
        epoch = _id("epoch")
        profile = formal_extract.extraction_profile(settings, epoch)
    if previous and not full and due and now < due and state.get("last_extraction_profile") == profile:
        return {"result": "not_due", "source_id": source_id, "model_calls": 0}
    run_id = _id("run")
    with storage.session(paths, readonly=False) as connection:
        connection.execute("""INSERT INTO crawl_runs
            (run_id,source_id,trigger,status,attempt,started_at) VALUES(?,?,?,?,?,?)""",
            (run_id, source_id, "manual", "RUNNING", 1, _now()))
    try:
        return _scan_started(source, state, previous, paths, run_id, profile, epoch, full)
    except Exception as exc:
        with storage.session(paths, readonly=False) as connection:
            _finish_run(connection, run_id, "FAILED", error_kind=type(exc).__name__, error_message=str(exc)[:2000])
            _check_event(connection, source_id, "FAILED", reason=str(exc)[:2000])
        raise


def storage_timestamp(value):
    if not value:
        return None
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _scan_started(source, state, previous, paths, run_id, profile, epoch, full):
    source_id = source["source_id"]
    config_hash = incremental.capture_config_hash(source)
    old_file = (_snapshot_file(paths, previous["markdown_path"]) if previous else None)
    can_reuse = bool(previous and previous["capture_config_hash"] == config_hash
                     and old_file.is_file())
    probe = None
    if can_reuse and not full:
        probe = incremental.conditional_probe(source, state)
        if probe.result in ("NOT_MODIFIED", "BODY_UNCHANGED"):
            if state.get("last_extraction_profile") == profile:
                return _record_unchanged(paths, source, run_id, state, previous, probe.result,
                                         probe, "HTTP validation / raw body hash")
            markdown = old_file.read_text(encoding="utf-8")
            return _process_markdown(source, state, previous, markdown, paths, run_id,
                                     profile, epoch, probe, full=False, crawled=False)
    result = asyncio.run(crawl.fetch_page(source))
    if not result.success:
        raise crawl.CrawlError("upstream_error", result.error_message or "Crawl4AI 抓取失败")
    final_url = getattr(result, "url", source["url"])
    if urlparse(final_url).scheme != "https" or urlparse(final_url).hostname != source["allowed_host"]:
        raise ValueError("浏览器被重定向到未登记的主机")
    markdown = crawl.extract_markdown(result)
    headers = {key.lower(): value for key, value in
               (getattr(result, "response_headers", None) or {}).items()}
    if probe is None:
        probe = incremental.Probe("CHANGED", headers.get("etag"),
                                  headers.get("last-modified"))
    elif headers.get("etag") and probe.etag and headers["etag"] != probe.etag:
        # The page changed between HTTP preflight and browser capture.
        probe = incremental.Probe("CHANGED", headers["etag"], headers.get("last-modified"))
    if len(" ".join(markdown.split())) < 200 or crawl.detect_blocked_page(markdown):
        raise ValueError("浏览器内容为空、过短或疑似拦截页")
    content_hash = incremental.normalized_hash(markdown)
    with storage.session(paths) as connection:
        snapshot = _row(connection, """SELECT * FROM source_snapshots
            WHERE source_id=? AND content_hash=? AND capture_config_hash=?""",
            (source_id, content_hash, config_hash))
    if not snapshot:
        snapshot_id = _id("snap")
        snapshot_dir = paths.snapshots / source_id
        snapshot_dir.mkdir(parents=True, exist_ok=True)
        md = snapshot_dir / f"{snapshot_id}.md"
        html = snapshot_dir / f"{snapshot_id}.html"
        md.write_text(markdown, encoding="utf-8")
        html.write_text(result.html or "", encoding="utf-8")
        with storage.session(paths, readonly=False) as connection:
            connection.execute("""INSERT INTO source_snapshots
                (snapshot_id,source_id,content_hash,capture_config_hash,capture_config_json,
                 final_url,html_path,markdown_path,captured_at) VALUES(?,?,?,?,?,?,?,?,?)""",
                (snapshot_id, source_id, content_hash, config_hash,
                 _json({"selector": source.get("content_selector"), "dynamic": bool(source.get("dynamic"))}),
                 final_url, str(html.relative_to(paths.data)), str(md.relative_to(paths.data)), _now()))
        snapshot = {"snapshot_id": snapshot_id, "source_id": source_id,
                    "content_hash": content_hash, "capture_config_hash": config_hash,
                    "markdown_path": str(md.relative_to(paths.data))}
    if (can_reuse and snapshot["snapshot_id"] == previous["snapshot_id"]
            and state.get("last_extraction_profile") == profile and not full):
        return _record_unchanged(paths, source, run_id, state, snapshot, "BODY_UNCHANGED",
                                 probe, "Crawl4AI Markdown hash")
    return _process_markdown(source, state, snapshot, markdown, paths, run_id,
                             profile, epoch, probe, full=full, crawled=True)


def _process_markdown(source, state, snapshot, markdown, paths, run_id,
                      profile, epoch, probe, *, full, crawled):
    source_id = source["source_id"]
    sections = incremental.split_sections(markdown)
    with storage.session(paths) as connection:
        previous = _current_snapshot(connection, state)
        old_sections = []
        if previous:
            old_file = _snapshot_file(paths, previous["markdown_path"])
            if old_file.is_file():
                old_sections = incremental.split_sections(old_file.read_text(encoding="utf-8"))
        dependencies = [tuple(row) for row in connection.execute("""SELECT section_key,depends_on_key
            FROM source_section_dependencies WHERE source_id=?""", (source_id,))]
        cached = {(row["section_hash"], row["extraction_profile"]): row["result_json"]
                  for row in connection.execute("""SELECT section_hash,extraction_profile,result_json
                    FROM section_extractions WHERE source_id=? AND extraction_profile=?""",
                    (source_id, profile))}
    affected, removed = incremental.changed_sections(old_sections, sections, dependencies)
    if full:
        affected = {section.key for section in sections}
    patches = []
    extracted = []
    reused = []
    model_calls = 0
    tokens = [0, 0]
    client = None
    settings = config.llm_settings()
    for section in sections:
        cache_key = (section.content_hash, profile)
        if section.key not in affected and cache_key in cached:
            patch = VisaPatch.model_validate_json(cached[cache_key])
            reused.append(section.key)
        else:
            if client is None:
                client, settings = formal_extract.legacy_extract._client()
            patch, usage = formal_extract.extract_section(section, source, client, settings)
            model_calls += 1
            tokens[0] += usage[0] or 0
            tokens[1] += usage[1] or 0
            extracted.append((section, patch))
        patches.append(patch)
    merged = formal_extract.merge_sections(patches)
    if not merged.policies:
        raise ValueError("整页没有抽取到签证政策字段；保留已发布知识并检查覆盖范围")
    report = gate.run_gate_visa(merged, markdown, source, {"excerpt_chars": len(markdown)})
    extraction_id = _id("ext")
    manifest = {"sections": [section.key for section in sections],
                "reused": reused, "extracted": [section.key for section, _ in extracted],
                "removed": sorted(removed), "profile": profile, "coverage": "complete",
                "snapshot_chars": len(markdown)}
    with storage.session(paths, readonly=False) as connection:
        # An unchanged snapshot can be reused by this run without duplicating files.
        connection.execute("UPDATE crawl_runs SET snapshot_id=? WHERE run_id=?",
                           (snapshot["snapshot_id"], run_id))
        for ordinal, section in enumerate(sections):
            connection.execute("""INSERT OR IGNORE INTO snapshot_sections
                (snapshot_id,source_id,section_key,heading,ordinal,content_hash,start_offset,end_offset)
                VALUES(?,?,?,?,?,?,?,?)""",
                (snapshot["snapshot_id"], source_id, section.key, section.heading, ordinal,
                 section.content_hash, section.start, section.end))
        for section, patch in extracted:
            connection.execute("""INSERT OR IGNORE INTO section_extractions
                (source_id,section_hash,extraction_profile,result_json,coverage_status,created_at)
                VALUES(?,?,?,?,?,?)""",
                (source_id, section.content_hash, profile, patch.model_dump_json(), "complete", _now()))
        connection.execute("""INSERT INTO extraction_runs
            (extraction_id,source_id,crawl_run_id,snapshot_id,method,model,prompt_version,
             schema_version,extractor_version,input_manifest_json,coverage_status,result_json,
             status,prompt_tokens,completion_tokens,started_at,finished_at)
             VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (extraction_id, source_id, run_id, snapshot["snapshot_id"], "llm", settings["model"],
             config.PROMPT_VERSIONS["visa_policy"], config.SCHEMA_VERSION,
             formal_extract.SECTION_EXTRACTOR_VERSION,
             _json(manifest), "complete", merged.model_dump_json(), "SUCCEEDED",
             tokens[0], tokens[1], _now(), _now()))
        candidates = _save_candidates(connection, source, merged, extraction_id, report)
        _finish_run(connection, run_id, "SUCCEEDED", snapshot["snapshot_id"])
        now = datetime.now(timezone.utc)
        # HTTP validators are trusted only for the representation that was checked.
        response_etag = probe.etag if probe else state.get("etag") if not crawled else None
        response_modified = probe.last_modified if probe else state.get("last_modified") if not crawled else None
        response_hash = probe.raw_body_hash if probe else state.get("raw_body_hash") if not crawled else None
        connection.execute("""INSERT INTO source_check_state
            (source_id,etag,last_modified,raw_body_hash,cache_epoch,last_extraction_profile,
             snapshot_id,last_checked_at,last_complete_at,last_full_at,next_check_at,last_result)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(source_id) DO UPDATE SET etag=excluded.etag,
            last_modified=excluded.last_modified,raw_body_hash=excluded.raw_body_hash,
            cache_epoch=excluded.cache_epoch,last_extraction_profile=excluded.last_extraction_profile,
            snapshot_id=excluded.snapshot_id,last_checked_at=excluded.last_checked_at,
            last_complete_at=excluded.last_complete_at,last_full_at=excluded.last_full_at,
            next_check_at=excluded.next_check_at,last_result=excluded.last_result""",
            (source_id, response_etag, response_modified, response_hash, epoch, profile,
             snapshot["snapshot_id"], now.isoformat(), now.isoformat(),
             now.isoformat() if full else state.get("last_full_at"), _next_due(source, now), "CHANGED"))
        _check_event(connection, source_id, "CHANGED", snapshot["snapshot_id"],
                     model_calls=model_calls, reused_sections=len(reused), candidates=candidates)
    return {"result": "candidates", "source_id": source_id, "run_id": run_id,
            "snapshot_id": snapshot["snapshot_id"], "model_calls": model_calls,
            "reused_sections": len(reused), "candidates": candidates}
