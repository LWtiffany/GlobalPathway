"""运行管道：租约 -> 采集 -> NO_CHANGE 短路 -> 抽取 -> 门控 -> 版本路由（设计 4.3）。"""
import asyncio
import sys
import time

from . import config, crawl as crawl_mod, db as db_mod, extract as extract_mod, gate as gate_mod, versions as versions_mod

# 可重试的故障类型（设计 A1.5 / 4.5：401/403、登录墙、验证码交由来源负责人处理，不重试）
RETRYABLE_KINDS = {"upstream_error", "upstream_timeout", "parse_failed",
                   "model_timeout", "model_unreachable", "model_rate_limit", "extraction_invalid"}


class PipelineError(Exception):
    def __init__(self, kind, message):
        super().__init__(message)
        self.kind = kind


def _backoff_minutes(attempt):
    return min(config.RETRY_BASE_MINUTES * (2 ** max(attempt - 1, 0)), config.RETRY_MAX_MINUTES)


def _next_attempt(db, source_id):
    last = db_mod.last_run(db, source_id)
    if last and last["status"] == "FAILED":
        return (last["attempt"] or 1) + 1
    return 1


def run_source_sync(source_id, trigger="manual", force=False, actor="cli"):
    """执行一次完整管道。返回 outcome dict；失败抛 PipelineError。"""
    config.ensure_dirs()
    with db_mod.session() as db:
        source = db_mod.get_source(db, source_id)
        if not source:
            raise PipelineError("config_error",
                                f"来源未登记：{source_id}（先执行 python agent1.py sources sync）")
        if not source["enabled"]:
            raise PipelineError("config_error", f"来源已停用：{source_id}")
        if source.get("review_status") != "approved":
            raise PipelineError("config_error", f"来源未获批准：{source_id}")
        lease = db_mod.active_lease(db, source_id)
        attempt = _next_attempt(db, source_id)
    if lease and not force:
        return {"result": "skipped", "reason": f"租约被 {lease['run_id']} 持有至 {lease['expires_at']}"}

    started = time.monotonic()
    with db_mod.session() as db:
        # create_run 自行生成 run_id 并落库；必须使用它返回的 id，
        # 否则后续 finish_run 更新 0 行，运行记录将永远停在 RUNNING
        run_id = db_mod.create_run(db, source_id, trigger, attempt=attempt)
        db_mod.acquire_lease(db, source_id, run_id)
    try:
        try:
            return _execute(source, run_id, started, trigger)
        except PipelineError:
            raise
        except Exception as exc:
            # 未分类错误也必须落 FAILED，避免运行记录停在 RUNNING（可观测性）
            with db_mod.session() as db:
                _fail(db, run_id, started, "internal", f"未分类错误：{exc}",
                      _current_attempt(db, run_id), source_id)
            raise
    finally:
        with db_mod.session() as db:
            db_mod.release_lease(db, source_id, run_id)


def _fail(db, run_id, started, kind, message, attempt, source_id):
    from datetime import datetime, timedelta, timezone
    retry_after = None
    if kind in RETRYABLE_KINDS:
        retry_after = (datetime.now(timezone.utc) + timedelta(minutes=_backoff_minutes(attempt))).isoformat()
    db_mod.finish_run(db, run_id, status="FAILED", error=message, error_kind=kind,
                      retry_after=retry_after, duration_ms=int((time.monotonic() - started) * 1000),
                      gate_result="failed")


def _execute(source, run_id, started, trigger):
    source_id = source["source_id"]
    schema_type = source.get("schema_type", "generic_facts")
    prompt_version = config.PROMPT_VERSIONS.get(schema_type, config.SCHEMA_VERSION)

    # 1. 采集（设计 4.3 步骤 2-3）
    try:
        snapshot = asyncio.run(crawl_mod.crawl(source))
    except crawl_mod.CrawlError as exc:
        with db_mod.session() as db:
            _fail(db, run_id, started, exc.kind, str(exc), _current_attempt(db, run_id), source_id)
        raise PipelineError(exc.kind, str(exc)) from exc

    # 2. NO_CHANGE 短路：已发布版本的页面、Schema 与提示词版本均未变化时，
    #    不重复调用模型（设计 4.3-2）。撤回/回滚后以发布指针为准，而非最新版本号。
    with db_mod.session() as db:
        pointer = db_mod.published_row(db, source_id)
        published = db_mod.get_version(db, pointer["version_id"]) if pointer else None
    if published and published["snapshot_hash"] == snapshot["page_hash"] \
            and published["schema_version"] == config.SCHEMA_VERSION \
            and published["prompt_version"] == prompt_version:
        with db_mod.session() as db:
            db_mod.touch_verified(db, published["version_id"])
            db_mod.finish_run(db, run_id, status="NO_CHANGE", snapshot_hash=snapshot["page_hash"],
                              gate_result="no_change", version_id=published["version_id"],
                              duration_ms=int((time.monotonic() - started) * 1000))
        return {"result": "no_change", "version_id": published["version_id"],
                "reason": "页面与处理版本均未变化，跳过模型调用并刷新核验时间"}

    # 3. 抽取（设计 4.3 步骤 4）
    try:
        patch, meta = extract_mod.extract(snapshot["markdown"], source)
    except extract_mod.ExtractionError as exc:
        with db_mod.session() as db:
            if exc.kind == "extraction_invalid":
                gate_mod.append_problems(
                    [{"check": "structure", "severity": "error",
                      "field": None, "value": None, "quote": None,
                      "reason": f"模型输出不符合 Schema：{exc}",
                      "suggested_action": "检查抽取提示词与模型输出；必要时人工复核或调整 Schema"}],
                    run_id=run_id, source_id=source_id, snapshot_hash=snapshot["page_hash"])
            _fail(db, run_id, started, exc.kind, str(exc), _current_attempt(db, run_id), source_id)
        raise PipelineError(exc.kind, str(exc)) from exc

    # 4. 质量门控（设计 4.3 步骤 5 / 4.4）
    report = gate_mod.run_gate(patch, snapshot["markdown"], source, meta)
    gate_mod.append_problems(report.problems, run_id=run_id, source_id=source_id,
                             snapshot_hash=snapshot["page_hash"])

    # 5. 版本与路由
    with db_mod.session() as db:
        outcome = versions_mod.process(db, source, snapshot, patch, meta, report, run_id)
        db_mod.finish_run(
            db, run_id, status="SUCCEEDED", snapshot_hash=snapshot["page_hash"],
            extraction_method=meta.get("method"), model=meta.get("model"),
            schema_version=meta.get("schema_version"), prompt_version=meta.get("prompt_version"),
            excerpt_chars=meta.get("excerpt_chars"), prompt_tokens=meta.get("prompt_tokens"),
            completion_tokens=meta.get("completion_tokens"), gate_result=outcome["result"],
            version_id=outcome.get("version_id"), review_id=outcome.get("review_id"),
            duration_ms=int((time.monotonic() - started) * 1000))
    # 问题单关联版本：与数据库事务分离且 best-effort——
    # 写 problems.json 失败（如沙箱禁止文件替换）不应回滚已成功的版本与运行记录
    if outcome.get("version_id"):
        try:
            gate_mod.link_run_to_version(run_id, outcome["version_id"])
        except OSError as exc:
            print(f"[WARN] 问题单关联版本失败（不影响本次运行结果）：{exc}", file=sys.stderr)
        _supersede_stale_problems(source_id, run_id)
    return outcome


def _supersede_stale_problems(source_id, run_id):
    """同来源旧运行的未关闭问题单被新版本取代时自动结算，避免问题单无限堆积。"""
    try:
        data = gate_mod.load_problems()
        stale = [p["problem_id"] for p in data["problems"]
                 if p.get("source_id") == source_id and p.get("status") == "open"
                 and p.get("run_id") != run_id]
        if stale:
            gate_mod.resolve_problems(
                problem_ids=set(stale),
                resolution="已被同来源新版本取代（新版本已生成并走审核/发布流程）")
    except OSError as exc:
        print(f"[WARN] 旧问题单结算失败（不影响本次运行结果）：{exc}", file=sys.stderr)


def _current_attempt(db, run_id):
    row = db.execute("SELECT attempt FROM agent_runs WHERE run_id=?", (run_id,)).fetchone()
    return row["attempt"] if row else 1


async def run_source(source_id, trigger="manual", force=False, actor="cli"):
    """调度器使用的异步入口（阻塞段放线程池）。"""
    return await asyncio.to_thread(run_source_sync, source_id, trigger=trigger, force=force, actor=actor)
