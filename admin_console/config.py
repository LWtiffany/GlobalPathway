"""Runtime configuration for the admin console.

Credentials never live in the repository. Everything is read from environment
variables (with defaults matching the SSH tunnel), and a git-ignored
``local_secrets.py`` may override ``ADMIN_DB_PASSWORD``.
"""
import os


def _env(name, default=None):
    value = os.environ.get(name)
    return value if value not in (None, "") else default


# PostgreSQL 连接默认走本地 SSH 隧道（127.0.0.1:15432 → ECS 内网 PostgreSQL），
# 让 dev 分支本地直接 `python -m admin_console serve` 即可连库，无需额外设环境变量。
# prod 分支部署在 ECS 上，由 docker-compose 的 env_file=.env 注入 ADMIN_DB_HOST/ADMIN_DB_PORT
# 覆盖为阿里云 RDS 内网地址（pgm-2ze9t2uh39o1115k.rwlb.rds.aliyuncs.com:5432）。
# 因此本文件的默认值在两个分支保持一致，合并代码时不会产生 config 差异/冲突。
DB_HOST = _env("ADMIN_DB_HOST", "127.0.0.1")
DB_PORT = int(_env("ADMIN_DB_PORT", "15432"))
DB_NAME = _env("ADMIN_DB_NAME", "team03")
DB_USER = _env("ADMIN_DB_USER", "team03")
DB_PASSWORD = _env("ADMIN_DB_PASSWORD", "")

# HTTP console
HOST = _env("ADMIN_HOST", "127.0.0.1")
PORT = int(_env("ADMIN_PORT", "8765"))

# admin login session lifetime
SESSION_SECONDS = 8 * 3600
MAX_NOTE = 500

try:
    from . import local_secrets  # git-ignored, never committed
    if getattr(local_secrets, "ADMIN_DB_PASSWORD", ""):
        DB_PASSWORD = local_secrets.ADMIN_DB_PASSWORD
except ImportError:
    pass
