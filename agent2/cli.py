"""Agent 2 命令行。

  serve [--host H] [--port P] [--knowledge agent1|fixture] [--agent1-env ENV]
                                            启动 S1 HTTP API（默认读 Agent1 已发布政策）
"""
import argparse
import os
import sys


def cmd_serve(args):
    from .api import serve
    from .service import Agent2Service
    from .store import Store

    if args.knowledge == "agent1":
        from .agent1_read import Agent1KnowledgeRead
        knowledge = Agent1KnowledgeRead.for_environment(args.agent1_env)
    else:
        from .knowledge import FixtureKnowledgeRead
        knowledge = FixtureKnowledgeRead.from_file()
    accounts = None
    try:
        from .accounts import RdsAccountDirectory
        accounts = RdsAccountDirectory()
        accounts.ensure_lock_columns()
    except Exception as exc:  # noqa: BLE001 — serve without password login if RDS unreachable
        print(f"warning: account directory unavailable ({exc}); /v1/login disabled", flush=True)
        accounts = None
    service = Agent2Service(Store(), knowledge,
                            demo_enabled=os.environ.get("AGENT2_DEMO", "1") == "1",
                            accounts=accounts)
    serve(service, args.host, args.port)
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(prog="agent2")
    sub = parser.add_subparsers(dest="command", required=True)
    agent1_env = os.environ.get("AGENT1_ENV", "development")
    p = sub.add_parser("serve", help="启动 S1 HTTP API")
    p.add_argument("--host", default=os.environ.get("AGENT2_HOST", "127.0.0.1"))
    p.add_argument("--port", type=int, default=int(os.environ.get("AGENT2_PORT", "8787")))
    p.add_argument("--knowledge", choices=("agent1", "fixture"), default="agent1",
                   help="agent1：读 Agent1 已发布政策；fixture：合成知识夹具")
    p.add_argument("--agent1-env", default=agent1_env)
    args = parser.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    return {"serve": cmd_serve}[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
