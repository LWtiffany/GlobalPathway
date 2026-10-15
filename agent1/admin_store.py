"""Admin database (admin.sqlite3, next to agent1.sqlite3): ADMIN_USER and REVIEW_QUEUE.

Agent1 sessions attach it as schema `admin` (storage.session), so approving a review
writes the domain table and deletes the queue row in one transaction.
"""
import hashlib
import hmac
import re
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from . import config, migrate

MIGRATIONS = Path(__file__).with_name("admin_migrations")
REQUIRED_MIGRATIONS = ("001_admin",)
PBKDF2_ITERATIONS = 600_000
MIN_PASSWORD = 8
USERNAME = re.compile(r"^[\w.-]{1,40}$")


def _connect(paths, mode):
    paths.validate()
    connection = sqlite3.connect(paths.admin_database.as_uri() + f"?mode={mode}", uri=True, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout=30000")
    return connection


def initialize(paths=None):
    paths = paths or config.PATHS
    paths.data.mkdir(parents=True, exist_ok=True)
    connection = _connect(paths, "rwc")
    try:
        connection.execute("BEGIN IMMEDIATE")
        migrate.apply(connection, MIGRATIONS)
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def attach(connection, paths, readonly):
    """Attach admin.sqlite3 as schema `admin` (must run before any transaction starts)."""
    if not paths.admin_database.exists():
        raise RuntimeError(f"缺少管理库 {paths.admin_database.name}；请先 db init。")
    mode = "ro" if readonly else "rw"
    connection.execute("ATTACH DATABASE ? AS admin", (paths.admin_database.as_uri() + f"?mode={mode}",))
    lacking = migrate.missing(connection, REQUIRED_MIGRATIONS, schema="admin")
    if lacking:
        raise RuntimeError(f"管理库缺少迁移：{', '.join(sorted(lacking))}；请先 db init。")


@contextmanager
def session(paths=None, readonly=True):
    """Standalone connection to admin.sqlite3 (account management)."""
    paths = paths or config.PATHS
    if not paths.admin_database.exists():
        raise RuntimeError(f"缺少管理库 {paths.admin_database.name}；请先 db init。")
    connection = _connect(paths, "ro" if readonly else "rw")
    try:
        lacking = migrate.missing(connection, REQUIRED_MIGRATIONS)
        if lacking:
            raise RuntimeError(f"管理库缺少迁移：{', '.join(sorted(lacking))}；请先 db init。")
        if not readonly:
            connection.execute("BEGIN IMMEDIATE")
        yield connection
        if not readonly:
            connection.commit()
    except Exception:
        if not readonly:
            connection.rollback()
        raise
    finally:
        connection.close()


# ---------- accounts ----------

def _now():
    return datetime.now(timezone.utc).isoformat()


def hash_password(password, *, iterations=None):
    iterations = iterations or PBKDF2_ITERATIONS
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return f"pbkdf2_sha256${iterations}${salt.hex()}${digest.hex()}"


def verify_password(password, stored):
    try:
        scheme, iterations, salt, digest = stored.split("$")
        if scheme != "pbkdf2_sha256":
            return False
        actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt), int(iterations))
    except ValueError:
        return False
    return hmac.compare_digest(actual.hex(), digest)


_DUMMY_HASH = None


def _check_credentials(username, password):
    if not isinstance(username, str) or not USERNAME.match(username):
        raise ValueError("用户名只能包含字母、数字、下划线、点和连字符，1–40 个字符")
    if not isinstance(password, str) or len(password) < MIN_PASSWORD:
        raise ValueError(f"密码至少 {MIN_PASSWORD} 个字符")


def add_user(connection, username, password):
    _check_credentials(username, password)
    try:
        connection.execute("INSERT INTO ADMIN_USER (username, password_hash, created_at) VALUES (?,?,?)",
                           (username, hash_password(password), _now()))
    except sqlite3.IntegrityError:
        raise ValueError(f"管理员已存在：{username}")


def set_password(connection, username, password):
    _check_credentials(username, password)
    updated = connection.execute("UPDATE ADMIN_USER SET password_hash=? WHERE username=?",
                                 (hash_password(password), username)).rowcount
    if not updated:
        raise ValueError(f"没有该管理员：{username}")


def remove_user(connection, username):
    if not connection.execute("DELETE FROM ADMIN_USER WHERE username=?", (username,)).rowcount:
        raise ValueError(f"没有该管理员：{username}")


def list_users(connection):
    return [dict(r) for r in connection.execute(
        "SELECT username, created_at, last_login_at FROM ADMIN_USER ORDER BY username")]


def authenticate(connection, username, password):
    """True when the credentials match; records last_login_at. Unknown users cost the same hash."""
    global _DUMMY_HASH
    row = connection.execute("SELECT password_hash FROM ADMIN_USER WHERE username=?",
                             (username,)).fetchone() if isinstance(username, str) else None
    if row is None:
        _DUMMY_HASH = _DUMMY_HASH or hash_password(secrets.token_hex(8))
        verify_password(str(password), _DUMMY_HASH)
        return False
    if not isinstance(password, str) or not verify_password(password, row["password_hash"]):
        return False
    connection.execute("UPDATE ADMIN_USER SET last_login_at=? WHERE username=?", (_now(), username))
    return True
