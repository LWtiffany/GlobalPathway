"""Agent 2 命令行。

  serve [--host H] [--port P] [--db PATH] [--knowledge agent1|fixture] [--agent1-env ENV]
                                            启动 S1 HTTP API（默认读 Agent1 已发布政策）；
                                            同址提供 /admin 审核后台
  admin add|passwd|remove|list [USERNAME] [--password-stdin] [--agent1-env ENV]
                                            管理审核后台账号（管理库 admin.sqlite3 的 ADMIN_USER）
  eval-m06 [--repeat N] [--out PATH]        跑 M06 分模块测试指标，保存逐条记录
"""
import argparse
import getpass
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data" / "agent2"


def _agent1_paths(environment):
    from agent1 import config as agent1_config
    from agent1.environment import paths_for
    return paths_for(agent1_config.ROOT, environment)


def cmd_serve(args):
    from .admin import AdminConsole
    from .api import serve
    from .service import Agent2Service
    from .store import Store

    if args.knowledge == "agent1":
        from .agent1_read import Agent1KnowledgeRead
        knowledge = Agent1KnowledgeRead.for_environment(args.agent1_env)
    else:
        from .knowledge import FixtureKnowledgeRead
        knowledge = FixtureKnowledgeRead.from_file()
    db = Path(args.db)
    db.parent.mkdir(parents=True, exist_ok=True)
    service = Agent2Service(Store(db), knowledge,
                            demo_enabled=os.environ.get("AGENT2_DEMO", "1") == "1",
                            admin=AdminConsole(_agent1_paths(args.agent1_env)))
    serve(service, args.host, args.port)
    return 0


def _read_password(from_stdin):
    if from_stdin:
        return sys.stdin.readline().rstrip("\n")
    first = getpass.getpass("密码：")
    if first != getpass.getpass("再输一次："):
        raise ValueError("两次输入的密码不一致")
    return first


def cmd_admin(args):
    from agent1 import admin_store

    paths = _agent1_paths(args.agent1_env)
    if args.admin_command != "list" and not args.username:
        print(f"admin {args.admin_command} 需要用户名", file=sys.stderr)
        return 2
    try:
        if args.admin_command == "list":
            with admin_store.session(paths) as db:
                print(json.dumps(admin_store.list_users(db), ensure_ascii=False, indent=2))
            return 0
        if args.admin_command == "remove":
            with admin_store.session(paths, readonly=False) as db:
                admin_store.remove_user(db, args.username)
            print(f"已删除管理员 {args.username}")
            return 0
        password = _read_password(args.password_stdin)
        with admin_store.session(paths, readonly=False) as db:
            if args.admin_command == "add":
                admin_store.add_user(db, args.username, password)
            else:
                admin_store.set_password(db, args.username, password)
    except (ValueError, RuntimeError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(f"已{'创建' if args.admin_command == 'add' else '更新'}管理员 {args.username}")
    return 0


def cmd_eval_m06(args):
    from . import eval_m06

    report = eval_m06.run(repeat=args.repeat)
    out = Path(args.out) if args.out else DATA / "eval" / f"m06-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    summary = {k: report[k] for k in ("fixture_version", "knowledge_fixture", "metrics", "passed", "ok")}
    summary["failed_cases"] = [{"case_id": r["case_id"], "errors": r["errors"]} for r in report["records"] if r["errors"]]
    summary["report"] = str(out)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 1


def main(argv=None):
    parser = argparse.ArgumentParser(prog="agent2")
    sub = parser.add_subparsers(dest="command", required=True)
    agent1_env = os.environ.get("AGENT1_ENV", "development")
    p = sub.add_parser("serve", help="启动 S1 HTTP API")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--db", default=str(DATA / "agent2.sqlite3"))
    p.add_argument("--knowledge", choices=("agent1", "fixture"), default="agent1",
                   help="agent1：读 Agent1 已发布政策；fixture：合成知识夹具")
    p.add_argument("--agent1-env", default=agent1_env)
    p = sub.add_parser("admin", help="审核后台账号管理")
    p.add_argument("admin_command", choices=("add", "passwd", "remove", "list"))
    p.add_argument("username", nargs="?")
    p.add_argument("--password-stdin", action="store_true", help="从标准输入读一行作为密码（脚本用）")
    p.add_argument("--agent1-env", default=agent1_env)
    p = sub.add_parser("eval-m06", help="M06 缺失条件判断与追问的测试指标")
    p.add_argument("--repeat", type=int, default=20, help="计时重复次数（P95 样本）")
    p.add_argument("--out")
    args = parser.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    return {"serve": cmd_serve, "admin": cmd_admin, "eval-m06": cmd_eval_m06}[args.command](args)
