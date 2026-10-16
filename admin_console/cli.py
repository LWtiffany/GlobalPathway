"""Command-line entry for the admin console.

  serve                             启动审核后台 HTTP 服务（127.0.0.1:8765）
  admin add|passwd|remove|list [USERNAME] [--password-stdin]
                                    管理审核后台账号（admin.admin_user）
"""
import argparse
import getpass
import json
import sys

from . import auth, config, db


def cmd_serve(args):
    from .console import AdminConsole
    from .server import serve
    serve(AdminConsole(), args.host, args.port)
    return 0


def _read_password(from_stdin):
    if from_stdin:
        return sys.stdin.readline().rstrip("\n")
    first = getpass.getpass("密码：")
    if first != getpass.getpass("再输一次："):
        raise ValueError("两次输入的密码不一致")
    return first


def cmd_admin(args):
    if args.admin_command != "list" and not args.username:
        print(f"admin {args.admin_command} 需要用户名", file=sys.stderr)
        return 2
    connection = db.connect_and_prepare()
    try:
        if args.admin_command == "list":
            print(json.dumps(auth.list_users(connection), ensure_ascii=False, indent=2, default=str))
            return 0
        if args.admin_command == "remove":
            auth.remove_user(connection, args.username)
            connection.commit()
            print(f"已删除管理员 {args.username}")
            return 0
        password = _read_password(args.password_stdin)
        if args.admin_command == "add":
            auth.add_user(connection, args.username, password)
        else:  # passwd
            auth.set_password(connection, args.username, password)
        connection.commit()
    except (ValueError, RuntimeError) as exc:
        connection.rollback()
        print(str(exc), file=sys.stderr)
        return 1
    finally:
        connection.close()
    print(f"已{'创建' if args.admin_command == 'add' else '更新'}管理员 {args.username}")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(prog="admin_console")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("serve", help="启动审核后台 HTTP 服务")
    p.add_argument("--host", default=config.HOST)
    p.add_argument("--port", type=int, default=config.PORT)
    p = sub.add_parser("admin", help="审核后台账号管理")
    p.add_argument("admin_command", choices=("add", "passwd", "remove", "list"))
    p.add_argument("username", nargs="?")
    p.add_argument("--password-stdin", action="store_true", help="从标准输入读一行作为密码")
    args = parser.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    return {"serve": cmd_serve, "admin": cmd_admin}[args.command](args)
