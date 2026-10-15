"""Agent1 本地管理 CLI。

命令：
  scan ID [--full]            新库：检查一个来源并重写其区域的 VISA_POLICY
  scan-region R [--full|--reapply]  新库：检查区域 R（UK / US / CN-HK / all）的全部签证来源
  scan-fx [C] [--full|--reapply]    新库：检查国家 C（默认 all）的全部外汇（留学购汇）来源，写 FX_POLICY
  scan-tuition [S] [--full|--reapply]  新库：检查学校 S（school key / 区域 / 默认 all）的学费来源，写 SCHOOL/PROGRAM/TUITION
  sources sync|list|set      来源登记（设计 2.4 / A1.1）
  run ID [ID...] [--force]    手动触发完整管道
  schedule [--once] [--source ID...]   进程内定时调度
  runs [--limit N]            运行记录与失败原因
  gate [--all] [--json]       门控问题清单（problems.json）
  gate resolve ID [ID...]     结算问题条目
  review list|show|approve|reject  人工审核队列（REVIEW_QUEUE）
  review issues [--status S] [--source ID]   门控问题清单（GATE_ISSUE）
  publish TARGET [--ack-warnings] [--reason R]   新库：人工发布签证政策（区域 / all / policy_id）
  publication list|preview|read|withdraw   新库：发布面查看、发布前预览、Agent2 视角读取、撤回
  withdraw TARGET --reason    紧急撤回
  rollback SOURCE --to VERSION --reason   回滚旧版本
  knowledge [--schema S] [--source ID] [--include-stale]   Agent2 只读知识接口
  status                      本地运营总览
"""
import argparse
import asyncio
import getpass
import json
import sqlite3
import sys

from . import config, db as db_mod, gate as gate_mod, knowledge as knowledge_mod, pipeline, scheduler, versions
from . import storage
from . import formal_pipeline, fx_pipeline, publication, tuition_pipeline
from .environment import ENVIRONMENTS, cleanup_test_storage


def _print_json(data):
    print(json.dumps(data, ensure_ascii=False, indent=2))


def _actor(name):
    return name or getpass.getuser() or "local"


def cmd_database(args):
    if args.db_command == "sync-sources":
        count = formal_pipeline.register_sources(args.file or config.SOURCES_PATH)
        _print_json({"environment": config.ENVIRONMENT, "sources_synced": count})
        return 0
    result = storage.initialize() if args.db_command == "init" else storage.status()
    _print_json(result)
    return 0


def cmd_scan(args):
    _print_json(formal_pipeline.scan(args.source_id, full=args.full))
    return 0


def cmd_scan_region(args):
    _print_json(formal_pipeline.scan_region(args.region, full=args.full, reapply=args.reapply))
    return 0


def cmd_scan_fx(args):
    _print_json(fx_pipeline.scan_all(args.country, full=args.full, reapply=args.reapply))
    return 0


def cmd_scan_tuition(args):
    _print_json(tuition_pipeline.scan_all(args.target, full=args.full, reapply=args.reapply))
    return 0


# ---------- sources ----------

def cmd_sources(args):
    with db_mod.session() as db:
        if args.sources_command == "sync":
            sources = db_mod.sync_registry(db, args.file)
            db_mod.audit(db, _actor(args.actor), "source_sync", "source_registry", "*",
                         {"count": len(sources), "file": str(args.file or config.SOURCES_PATH)})
            print(f"已同步 {len(sources)} 个来源到登记表")
            for source in sources:
                print(f"  - {source.source_id} [{source.category}/{source.schema_type}] "
                      f"enabled={source.enabled} interval={source.check_interval_minutes}min "
                      f"sla={source.freshness_sla_hours}h")
            return 0
        if args.sources_command == "set":
            fields = {}
            if args.interval_min is not None:
                fields["check_interval_minutes"] = args.interval_min
            if args.sla_hours is not None:
                fields["freshness_sla_hours"] = args.sla_hours
            if args.enable:
                fields["enabled"] = 1
            if args.disable:
                fields["enabled"] = 0
            if args.owner is not None:
                fields["owner"] = args.owner
            if args.max_retries is not None:
                fields["max_retries"] = args.max_retries
            if not fields:
                print("没有需要更新的字段", file=sys.stderr)
                return 2
            if not db_mod.update_source(db, args.source_id, **fields):
                print(f"来源不存在：{args.source_id}", file=sys.stderr)
                return 1
            db_mod.audit(db, _actor(args.actor), "source_update", "source", args.source_id, fields)
            print(f"已更新 {args.source_id}: {fields}")
            return 0
        sources = db_mod.list_sources(db, enabled_only=not args.all)
        if args.json:
            _print_json(sources)
            return 0
        for source in sources:
            last = db_mod.last_run(db, source["source_id"])
            last_desc = f"{last['status']}@{last['started_at'][:19]}" if last else "从未运行"
            print(f"{source['source_id']:<28} {source['category']:<10} {source['schema_type']:<14} "
                  f"enabled={source['enabled']} interval={source['check_interval_minutes']}min "
                  f"sla={source['freshness_sla_hours']}h  最近：{last_desc}")
        return 0


# ---------- run ----------

def cmd_run(args):
    failures = 0
    for source_id in args.source_ids:
        try:
            outcome = pipeline.run_source_sync(source_id, trigger="manual", force=args.force,
                                               actor=_actor(args.actor))
            _print_json(outcome)
        except pipeline.PipelineError as exc:
            failures += 1
            print(json.dumps({"source": source_id, "result": "failed", "error_kind": exc.kind,
                              "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
    return 1 if failures == len(args.source_ids) else 0


# ---------- schedule ----------

def cmd_schedule(args):
    print(f"[scheduler] 启动（once={args.once}, interval={args.interval_sec}s, "
          f"filter={args.source or '全部启用来源'}）")
    asyncio.run(scheduler.schedule(source_filter=set(args.source) if args.source else None,
                                   once=args.once, interval_sec=args.interval_sec))
    return 0


# ---------- runs ----------

def cmd_runs(args):
    with db_mod.session() as db:
        runs = db_mod.recent_runs(db, limit=args.limit, source_id=args.source)
    if args.json:
        _print_json(runs)
        return 0
    for run in runs:
        line = (f"{run['started_at'][:19]}  {run['source_id']:<28} {run['status']:<10} "
                f"attempt={run['attempt']} gate={run['gate_result'] or '-'} "
                f"method={run['extraction_method'] or '-'} {run['duration_ms'] or 0}ms")
        print(line)
        if run["error"]:
            print(f"    error[{run['error_kind']}]: {run['error']}")
        if run.get("retry_after"):
            print(f"    计划重试：{run['retry_after'][:19]}")
    return 0


# ---------- gate ----------

def cmd_gate(args):
    data = gate_mod.load_problems()
    problems = data.get("problems", [])
    if args.gate_command == "resolve":
        resolved = gate_mod.resolve_problems(problem_ids=set(args.problem_ids),
                                             resolution=f"manual by {_actor(args.actor)}")
        print(f"已结算 {len(resolved)} 条问题")
        for problem_id in resolved:
            print(f"  - {problem_id}")
        return 0
    shown = problems if args.all else [p for p in problems if p.get("status") == "open"]
    if args.json:
        _print_json(shown)
        return 0
    if not shown:
        print("没有问题条目")
        return 0
    for problem in shown:
        print(f"[{problem['severity']:<7}] {problem['check']:<13} {problem['source_id']:<28} "
              f"{problem.get('field') or '-'}")
        print(f"    原因：{problem['reason']}")
        if problem.get("value"):
            print(f"    取值：{problem['value']}")
        if problem.get("quote"):
            print(f"    引用：{problem['quote']}")
        print(f"    建议：{problem['suggested_action']}")
        print(f"    条目：{problem['problem_id']}  run={problem['run_id']}  at={problem['at'][:19]}"
              f"{'  version=' + problem['version_id'] if problem.get('version_id') else ''}")
        print()
    open_count = sum(1 for p in problems if p.get("status") == "open")
    print(f"共 {len(shown)} 条（未结算 {open_count} 条），文件：{config.PROBLEMS_PATH}")
    return 0


# ---------- review (REVIEW_QUEUE in the admin database) ----------

def cmd_review(args):
    paths = config.PATHS
    if args.review_command == "issues":
        sql = "SELECT * FROM GATE_ISSUE WHERE status=?"
        parameters = [args.status]
        if args.source:
            sql += " AND source_id=?"
            parameters.append(args.source)
        sql += " ORDER BY source_id, severity, scope, check_name"
        with storage.session(paths) as db:
            rows = [dict(r) for r in db.execute(sql, parameters)]
        if args.json:
            _print_json(rows)
            return 0
        if not rows:
            print(f"没有 {args.status} 状态的门控问题")
            return 0
        for row in rows:
            print(f"[{row['severity']:<7}] {row['check_name']:<13} {row['source_id']:<24} "
                  f"scope={row['scope'] or '-'}  field={row['field'] or '-'}")
            print(f"    原因：{row['reason']}")
            if row.get("value"):
                print(f"    取值：{row['value']}")
            if row.get("quote"):
                print(f"    引用：{row['quote']}")
            if row.get("suggested_action"):
                print(f"    建议：{row['suggested_action']}")
            print(f"    条目：{row['issue_id']}  first={row['first_seen_at'][:19]}")
        print(f"\n共 {len(rows)} 条（{args.status}）")
        return 0
    if args.review_command == "list":
        with storage.session(paths) as db:
            rows = [dict(r) for r in db.execute("SELECT * FROM admin.REVIEW_QUEUE ORDER BY created_at")]
            open_issues = db.execute(
                "SELECT COUNT(*) FROM GATE_ISSUE WHERE status='OPEN'").fetchone()[0]
        if args.json:
            _print_json(rows)
            return 0
        if not rows:
            print("没有待审核项")
            return 0
        for row in rows:
            print(f"{row['review_id']}  {row['domain_table']}.{row['record_key'][:13]}…  "
                  f"{row['action']:<6} at={row['created_at'][:19]}")
            print(f"    原因：{row['reason'][:160]}")
        if open_issues:
            print(f"\n另：当前有 {open_issues} 条未解决的门控问题，用 `review issues` 查看。")
        return 0
    if not args.review_id:
        print(f"{args.review_command} 需要 review_id", file=sys.stderr)
        return 2
    if args.review_command == "show":
        with storage.session(paths) as db:
            row = db.execute("SELECT * FROM admin.REVIEW_QUEUE WHERE review_id=?",
                             (args.review_id,)).fetchone()
        if not row:
            print(f"没有审核项：{args.review_id}", file=sys.stderr)
            return 1
        _print_json(dict(row))
        return 0
    if args.review_command == "approve":
        with storage.session(paths, readonly=False) as db:
            result = formal_pipeline.apply_review(db, args.review_id,
                                                  decided_by=_actor(args.actor),
                                                  note=args.note)
        _print_json(result)
        record = result.get("policy_id") or result.get("tuition_id")
        print(f"已批准并写入 {record}（{len(result['applied_fields'])} 个字段），审核项已移出队列")
        return 0
    if args.review_command == "reject":
        if not args.note:
            print("退回必须填写理由（--note）", file=sys.stderr)
            return 2
        with storage.session(paths, readonly=False) as db:
            result = formal_pipeline.reject_review(db, args.review_id,
                                                   decided_by=_actor(args.actor), note=args.note)
        _print_json(result)
        print("已驳回，数据未入库，审核项已移出队列")
        return 0
    return 0


# ---------- publication (formal database) ----------

def cmd_publish(args):
    results = publication.publish(args.target, actor=_actor(args.actor), reason=args.reason,
                                  ack_warnings=args.ack_warnings)
    _print_json(results)
    return 1 if any(r["result"] == "refused" for r in results) else 0


def cmd_publication(args):
    if args.publication_command == "list":
        _print_json(publication.list_publications())
        return 0
    if args.publication_command == "read":
        _print_json(publication.search(args.target))
        return 0
    if not args.target:
        print(f"publication {args.publication_command} 需要目标", file=sys.stderr)
        return 2
    if args.publication_command == "preview":
        _print_json(publication.preview(args.target))
        return 0
    _print_json(publication.withdraw(args.target, actor=_actor(args.actor), reason=args.reason))
    return 0


# ---------- withdraw / rollback ----------

def cmd_withdraw(args):
    with db_mod.session() as db:
        version_id = versions.withdraw(db, args.target, args.reason, _actor(args.actor))
    print(f"已撤回：{version_id}")
    return 0


def cmd_rollback(args):
    with db_mod.session() as db:
        version_id = versions.rollback(db, args.source_id, args.to, args.reason,
                                       _actor(args.actor), force=args.force)
    print(f"已回滚到：{version_id}")
    return 0


# ---------- knowledge ----------

def cmd_knowledge(args):
    records = knowledge_mod.query(schema_type=args.schema, source_id=args.source,
                                  include_stale=args.include_stale)
    _print_json(records)
    return 0


# ---------- status ----------

def cmd_status(args):
    with db_mod.session() as db:
        sources = db_mod.list_sources(db)
        runs = db_mod.recent_runs(db, limit=10)
        reviews = db_mod.list_reviews(db, status="OPEN")
        last_runs = {source["source_id"]: db_mod.last_run(db, source["source_id"]) for source in sources}
        published = {row["source_id"]: dict(row) for row in
                     db.execute("SELECT * FROM published_knowledge").fetchall()}
    problems = gate_mod.load_problems().get("problems", [])
    open_problems = [p for p in problems if p.get("status") == "open"]
    errors = [p for p in open_problems if p["severity"] == "error"]
    warnings = [p for p in open_problems if p["severity"] == "warning"]

    print("== 来源 ==")
    for source in sources:
        last = last_runs.get(source["source_id"])
        pointer = published.get(source["source_id"])
        last_desc = f"{last['status']}@{last['started_at'][:19]}" if last else "从未运行"
        print(f"  {source['source_id']:<28} enabled={source['enabled']}  最近运行：{last_desc}  "
              f"已发布：{pointer['version_id'] if pointer else '-'}")
    print("\n== 最近运行 ==")
    for run in runs:
        print(f"  {run['started_at'][:19]}  {run['source_id']:<28} {run['status']:<10} "
              f"gate={run['gate_result'] or '-'} {run['duration_ms'] or 0}ms")
        if run["error"]:
            print(f"      error[{run['error_kind']}]: {run['error']}")
    print("\n== 审核队列 ==")
    if reviews:
        for review in reviews:
            print(f"  {review['review_id']}  {review['source_id']:<28} {review['reason']}")
    else:
        print("  空")
    print("\n== 门控问题 ==")
    print(f"  未结算 {len(open_problems)} 条（error {len(errors)} / warning {len(warnings)}），"
          f"文件：{config.PROBLEMS_PATH}")
    print("  查看：python agent1.py gate")
    return 0


# ---------- 参数解析 ----------

def build_parser():
    parser = argparse.ArgumentParser(prog="agent1", description="Agent1 公共知识采集与发布（本地部署）")
    parser.add_argument("--env", choices=ENVIRONMENTS, default=config.ENVIRONMENT,
                        help="数据环境；默认 AGENT1_ENV 或 development。放在子命令前")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("db", help="新数据库结构管理（不采集、不迁移旧数据）")
    p.add_argument("db_command", choices=["init", "status", "sync-sources"])
    p.add_argument("--file", help="sync-sources 使用的来源模板文件")

    p = sub.add_parser("scan", help="新库：增量检查一个来源并重写其所属区域的签证/外汇政策；不发布")
    p.add_argument("source_id")
    p.add_argument("--full", action="store_true", help="强制整页抓取和完整重新抽取")

    p = sub.add_parser("scan-region", help="新库：检查一个区域的全部签证来源（UK / US / CN-HK / all）")
    p.add_argument("region")
    p.add_argument("--full", action="store_true", help="强制整页抓取和完整重新抽取")
    p.add_argument("--reapply", action="store_true",
                   help="不联网：用已存快照和缓存抽取，按当前合并/门控规则重新入库")

    p = sub.add_parser("scan-fx", help="新库：检查外汇（留学购汇）来源并重写 FX_POLICY；不发布")
    p.add_argument("country", nargs="?", default="all", help="国家（sources.json region，如 CN）；默认 all")
    p.add_argument("--full", action="store_true", help="强制整页抓取和完整重新抽取")
    p.add_argument("--reapply", action="store_true",
                   help="不联网：用已存快照和缓存抽取，按当前合并/门控规则重新入库")

    p = sub.add_parser("scan-tuition", help="新库：检查学费来源并写 SCHOOL/PROGRAM/TUITION；不发布")
    p.add_argument("target", nargs="?", default="all",
                   help="学校（sources.json school.key，如 ucl）或区域（UK / CN-HK）；默认 all")
    p.add_argument("--full", action="store_true", help="强制整页抓取和完整重新抽取")
    p.add_argument("--reapply", action="store_true",
                   help="不联网：用已存快照和缓存抽取，按当前合并/门控规则重新入库")

    p = sub.add_parser("sources", help="来源登记管理")
    p.add_argument("sources_command", choices=["sync", "list", "set"])
    p.add_argument("source_id", nargs="?", help="set 时的来源 ID")
    p.add_argument("--file", help="sources sync 的 JSON 文件路径")
    p.add_argument("--interval-min", type=int)
    p.add_argument("--sla-hours", type=int)
    p.add_argument("--max-retries", type=int)
    p.add_argument("--owner")
    p.add_argument("--enable", action="store_true")
    p.add_argument("--disable", action="store_true")
    p.add_argument("--all", action="store_true", help="list 时包含停用来源")
    p.add_argument("--json", action="store_true")
    p.add_argument("--actor")

    p = sub.add_parser("run", help="手动触发一个或多个来源的完整管道")
    p.add_argument("source_ids", nargs="+")
    p.add_argument("--force", action="store_true", help="忽略租约强制执行")
    p.add_argument("--actor")

    p = sub.add_parser("schedule", help="进程内定时调度")
    p.add_argument("--once", action="store_true", help="只跑一轮到期来源后退出")
    p.add_argument("--source", action="append", help="限定来源（可多次）")
    p.add_argument("--interval-sec", type=int, default=30)
    p.add_argument("--actor")

    p = sub.add_parser("runs", help="运行记录")
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--source")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("gate", help="门控问题清单")
    p.add_argument("gate_command", nargs="?", default="list", choices=["list", "resolve"])
    p.add_argument("problem_ids", nargs="*")
    p.add_argument("--all", action="store_true", help="包含已结算条目")
    p.add_argument("--json", action="store_true")
    p.add_argument("--actor")

    p = sub.add_parser("review", help="人工审核（REVIEW_QUEUE）与门控问题（GATE_ISSUE）")
    p.add_argument("review_command", choices=["list", "show", "approve", "reject", "issues"])
    p.add_argument("review_id", nargs="?", help="show/approve/reject 时必填")
    p.add_argument("--note")
    p.add_argument("--status", default="OPEN", help="issues 时按门控问题状态过滤")
    p.add_argument("--source", help="issues 时按来源过滤")
    p.add_argument("--json", action="store_true")
    p.add_argument("--actor")

    p = sub.add_parser("publish", help="新库：发布前检查通过后封存修订并发布（人工操作）")
    p.add_argument("target", help="区域（UK / US / CN-HK）、all 或 policy_id")
    p.add_argument("--ack-warnings", action="store_true", help="已逐条核对未解决的门控警告")
    p.add_argument("--reason")
    p.add_argument("--actor")

    p = sub.add_parser("publication", help="新库：发布面")
    p.add_argument("publication_command", choices=["list", "preview", "read", "withdraw"])
    p.add_argument("target", nargs="?", help="preview：区域/all/policy_id；read：国家；withdraw：policy_id")
    p.add_argument("--reason")
    p.add_argument("--actor")

    p = sub.add_parser("withdraw", help="紧急撤回已发布知识")
    p.add_argument("target", help="version_id 或 source_id")
    p.add_argument("--reason", required=True)
    p.add_argument("--actor")

    p = sub.add_parser("rollback", help="回滚到旧版本")
    p.add_argument("source_id")
    p.add_argument("--to", required=True, help="目标 version_id")
    p.add_argument("--reason", required=True)
    p.add_argument("--force", action="store_true", help="旧版本超新鲜度仍强制回滚")
    p.add_argument("--actor")

    p = sub.add_parser("knowledge", help="Agent2 只读知识接口")
    p.add_argument("--schema")
    p.add_argument("--source")
    p.add_argument("--include-stale", action="store_true")

    p = sub.add_parser("status", help="本地运营总览")

    # 兼容旧命令
    p = sub.add_parser("visa", help="（兼容）等价于 run")
    p.add_argument("source_ids", nargs="+")
    p.add_argument("--force", action="store_true")
    p.add_argument("--actor")
    p = sub.add_parser("show", help="（兼容）等价于 status")
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        config.configure_environment(args.env)
        formal = ("db", "scan", "scan-region", "scan-fx", "scan-tuition", "review", "publish", "publication")
        target = config.DB_PATH if args.command in formal else config.LEGACY_DB_PATH
        print(f"[agent1] environment={config.ENVIRONMENT} database={target}", file=sys.stderr)
        if args.command == "db":
            return cmd_database(args)
        if args.command == "scan":
            return cmd_scan(args)
        if args.command == "scan-region":
            return cmd_scan_region(args)
        if args.command == "scan-fx":
            return cmd_scan_fx(args)
        if args.command == "scan-tuition":
            return cmd_scan_tuition(args)
        if args.command == "review":
            return cmd_review(args)
        if args.command == "publish":
            return cmd_publish(args)
        if args.command == "publication":
            return cmd_publication(args)
        config.require_legacy_environment()
        print("[agent1] 兼容管道：使用独立 legacy 库，尚未接入新正式表结构。", file=sys.stderr)
        return _dispatch(args)
    except (ValueError, KeyError, RuntimeError, sqlite3.Error) as exc:
        print(f"[agent1] {exc}", file=sys.stderr)
        return 2
    finally:
        if config.ENVIRONMENT == "test":
            cleanup_test_storage()


def _dispatch(args):
    if args.command == "visa":
        args.source_ids = args.source_ids
        return cmd_run(args)
    if args.command == "show":
        return cmd_status(args)
    handler = {
        "sources": cmd_sources, "run": cmd_run, "schedule": cmd_schedule, "runs": cmd_runs,
        "gate": cmd_gate, "review": cmd_review, "withdraw": cmd_withdraw,
        "rollback": cmd_rollback, "knowledge": cmd_knowledge, "status": cmd_status,
    }[args.command]
    return handler(args) or 0
