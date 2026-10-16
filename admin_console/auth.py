"""Administrator account authentication (PBKDF2-SHA256) against admin.admin_user.

Ported from Agent1's admin_store — same hash format, but PostgreSQL-backed.
"""
import hashlib
import hmac
import re
import secrets
from datetime import datetime, timezone

import psycopg

PBKDF2_ITERATIONS = 600_000
MIN_PASSWORD = 8
USERNAME = re.compile(r"^[\w.-]{1,40}$")

_DUMMY_HASH = None


def _now():
    return datetime.now(timezone.utc)


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
        actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                                     bytes.fromhex(salt), int(iterations))
    except ValueError:
        return False
    return hmac.compare_digest(actual.hex(), digest)


def _check_credentials(username, password):
    if not isinstance(username, str) or not USERNAME.match(username):
        raise ValueError("用户名只能包含字母、数字、下划线、点和连字符，1–40 个字符")
    if not isinstance(password, str) or len(password) < MIN_PASSWORD:
        raise ValueError(f"密码至少 {MIN_PASSWORD} 个字符")


def add_user(connection, username, password):
    _check_credentials(username, password)
    try:
        connection.execute(
            "INSERT INTO admin.admin_user (username, password_hash, created_at) VALUES (%s,%s,%s)",
            (username, hash_password(password), _now()))
    except psycopg.errors.UniqueViolation:
        connection.rollback()
        raise ValueError(f"管理员已存在：{username}")


def set_password(connection, username, password):
    _check_credentials(username, password)
    cur = connection.execute(
        "UPDATE admin.admin_user SET password_hash=%s WHERE username=%s",
        (hash_password(password), username))
    if cur.rowcount == 0:
        raise ValueError(f"没有该管理员：{username}")


def remove_user(connection, username):
    cur = connection.execute("DELETE FROM admin.admin_user WHERE username=%s", (username,))
    if cur.rowcount == 0:
        raise ValueError(f"没有该管理员：{username}")


def list_users(connection):
    rows = connection.execute(
        "SELECT username, created_at, last_login_at FROM admin.admin_user ORDER BY username").fetchall()
    return [{"username": r["username"],
             "created_at": r["created_at"].isoformat() if r["created_at"] else None,
             "last_login_at": r["last_login_at"].isoformat() if r["last_login_at"] else None}
            for r in rows]


def authenticate(connection, username, password):
    """True when the credentials match; records last_login_at. Unknown users cost the same hash."""
    global _DUMMY_HASH
    row = connection.execute("SELECT password_hash FROM admin.admin_user WHERE username=%s",
                             (username,)).fetchone() if isinstance(username, str) else None
    if row is None:
        _DUMMY_HASH = _DUMMY_HASH or hash_password(secrets.token_hex(8))
        verify_password(str(password), _DUMMY_HASH)
        return False
    if not isinstance(password, str) or not verify_password(password, row["password_hash"]):
        return False
    connection.execute("UPDATE admin.admin_user SET last_login_at=%s WHERE username=%s",
                       (_now(), username))
    return True
