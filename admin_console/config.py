"""Runtime configuration for the admin console.

Credentials never live in the repository. Everything is read from environment
variables (with defaults matching the SSH tunnel), and a git-ignored
``local_secrets.py`` may override ``ADMIN_DB_PASSWORD``.
"""
import os


def _env(name, default=None):
    value = os.environ.get(name)
    return value if value not in (None, "") else default


# PostgreSQL (Aliyun RDS, reachable over the competition ECS private network).
# Local development can override ADMIN_DB_HOST/ADMIN_DB_PORT to route through an SSH tunnel.
DB_HOST = _env("ADMIN_DB_HOST", "pgm-2ze9t2uh39o1115k.rwlb.rds.aliyuncs.com")
DB_PORT = int(_env("ADMIN_DB_PORT", "5432"))
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
