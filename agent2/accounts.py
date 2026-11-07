"""End-user account directory backed by RDS userinformation.users."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Protocol

from .passwords import hash_password

LOCK_AFTER_FAILURES = 5
LOCK_SECONDS = 15 * 60


def _utcnow():
    return datetime.now(timezone.utc)


class AccountDirectory(Protocol):
    def get_by_username(self, username: str): ...
    def record_failure(self, user_id: str) -> None: ...
    def clear_failures(self, user_id: str) -> None: ...
    def update_password_hash(self, user_id: str, password_hash: str) -> None: ...
    def ensure_lock_columns(self) -> None: ...


class MemoryAccountDirectory:
    """In-memory directory for unit tests (no RDS)."""

    def __init__(self, users=None):
        self._users = {}
        for user in users or []:
            self._users[user["username"]] = dict(user)

    def ensure_lock_columns(self) -> None:
        return None

    def get_by_username(self, username: str):
        row = self._users.get(username)
        return dict(row) if row else None

    def record_failure(self, user_id: str) -> None:
        for row in self._users.values():
            if row["user_id"] == user_id:
                row["failed_attempts"] = int(row.get("failed_attempts") or 0) + 1
                if row["failed_attempts"] >= LOCK_AFTER_FAILURES:
                    row["locked_until"] = _utcnow().timestamp() + LOCK_SECONDS
                return

    def clear_failures(self, user_id: str) -> None:
        for row in self._users.values():
            if row["user_id"] == user_id:
                row["failed_attempts"] = 0
                row["locked_until"] = None
                return

    def update_password_hash(self, user_id: str, password_hash: str) -> None:
        for row in self._users.values():
            if row["user_id"] == user_id:
                row["password_hash"] = password_hash
                row["password_updated_at"] = _utcnow()
                return


class RdsAccountDirectory:
    """Reads/writes userinformation.users via the shared Agent1 RDS connection."""

    def ensure_lock_columns(self) -> None:
        from agent1 import storage
        with storage.session(readonly=False) as db:
            db.execute("""
                ALTER TABLE userinformation.users
                ADD COLUMN IF NOT EXISTS failed_attempts INTEGER NOT NULL DEFAULT 0
            """)
            db.execute("""
                ALTER TABLE userinformation.users
                ADD COLUMN IF NOT EXISTS locked_until TIMESTAMPTZ
            """)

    def get_by_username(self, username: str):
        from agent1 import storage
        with storage.read_session() as db:
            row = db.execute(
                """SELECT user_id, username, password_hash, external_subject, status,
                          password_updated_at, created_at, updated_at,
                          COALESCE(failed_attempts, 0) AS failed_attempts,
                          locked_until
                   FROM userinformation.users WHERE username=%s""",
                (username,),
            ).fetchone()
        return dict(row) if row else None

    def record_failure(self, user_id: str) -> None:
        from agent1 import storage
        with storage.session(readonly=False) as db:
            db.execute(
                """UPDATE userinformation.users
                   SET failed_attempts = COALESCE(failed_attempts, 0) + 1,
                       locked_until = CASE
                         WHEN COALESCE(failed_attempts, 0) + 1 >= %s
                         THEN NOW() + (%s || ' seconds')::interval
                         ELSE locked_until
                       END,
                       updated_at = NOW()
                   WHERE user_id=%s""",
                (LOCK_AFTER_FAILURES, str(LOCK_SECONDS), user_id),
            )

    def clear_failures(self, user_id: str) -> None:
        from agent1 import storage
        with storage.session(readonly=False) as db:
            db.execute(
                """UPDATE userinformation.users
                   SET failed_attempts=0, locked_until=NULL, updated_at=NOW()
                   WHERE user_id=%s""",
                (user_id,),
            )

    def update_password_hash(self, user_id: str, password_hash: str) -> None:
        from agent1 import storage
        with storage.session(readonly=False) as db:
            db.execute(
                """UPDATE userinformation.users
                   SET password_hash=%s, password_updated_at=NOW(), updated_at=NOW()
                   WHERE user_id=%s""",
                (password_hash, user_id),
            )


def reset_all_passwords(password: str, directory: RdsAccountDirectory | None = None) -> int:
    """Overwrite every userinformation.users password_hash (destructive)."""
    from agent1 import storage
    directory = directory or RdsAccountDirectory()
    directory.ensure_lock_columns()
    with storage.session(readonly=False) as db:
        rows = db.execute("SELECT user_id FROM userinformation.users").fetchall()
        for row in rows:
            db.execute(
                """UPDATE userinformation.users
                   SET password_hash=%s, password_updated_at=NOW(), updated_at=NOW(),
                       failed_attempts=0, locked_until=NULL
                   WHERE user_id=%s""",
                (hash_password(password), row["user_id"]),
            )
        return len(rows)
