"""本地调度器：到期检查、租约防重、失败退避重试（设计 3.1 / 7.4 / A1.5）。

Demo 形态为进程内 asyncio 定时器 + SQLite 任务记录与租约；
不引入独立消息队列，后续可将 agent_runs 迁移至队列而不改管道契约。
"""
import asyncio
import sys
from datetime import datetime, timedelta, timezone

from . import config, db as db_mod, pipeline


def _parse(ts):
    return db_mod.parse_ts(ts)


def due_sources(now=None):
    now = now or datetime.now(timezone.utc)
    due = []
    with db_mod.session() as db:
        for source in db_mod.list_sources(db, enabled_only=True):
            if db_mod.active_lease(db, source["source_id"], now):
                continue
            last = db_mod.last_run(db, source["source_id"])
            if not last:
                due.append(source)
                continue
            if last["status"] == "RUNNING":
                started = _parse(last["started_at"])
                # 无租约的孤立运行超过租约时长后允许接管
                if started and now - started > timedelta(minutes=config.LEASE_MINUTES):
                    due.append(source)
                continue
            base = _parse(last["finished_at"]) or _parse(last["started_at"])
            if base is None:
                due.append(source)
                continue
            if last["status"] == "FAILED":
                retryable = (last.get("error_kind") in pipeline.RETRYABLE_KINDS
                             and (last["attempt"] or 1) < source["max_retries"])
                if retryable:
                    retry_after = _parse(last.get("retry_after"))
                    if retry_after and now < retry_after:
                        continue
                    due.append(source)
                    continue
            if now >= base + timedelta(minutes=source["check_interval_minutes"]):
                due.append(source)
    return due


async def schedule(source_filter=None, once=False, interval_sec=30, actor="scheduler"):
    while True:
        try:
            due = due_sources()
        except Exception as exc:  # 调度器本身故障不应中断循环
            print(f"[scheduler] 到期计算失败：{exc}", file=sys.stderr)
            due = []
        for source in due:
            if source_filter and source["source_id"] not in source_filter:
                continue
            print(f"[scheduler] {datetime.now(timezone.utc).isoformat()} 触发 {source['source_id']}")
            try:
                outcome = await pipeline.run_source(source["source_id"], trigger="schedule", actor=actor)
                print(f"[scheduler] {source['source_id']} -> {outcome.get('result')}"
                      f"{' (' + outcome['reason'] + ')' if outcome.get('reason') else ''}")
            except pipeline.PipelineError as exc:
                print(f"[scheduler] {source['source_id']} 失败[{exc.kind}]：{exc}", file=sys.stderr)
            except Exception as exc:
                print(f"[scheduler] {source['source_id']} 异常：{exc}", file=sys.stderr)
        if once:
            return
        await asyncio.sleep(interval_sec)
