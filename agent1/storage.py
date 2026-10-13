"""Formal schema lifecycle for the clean domain schema (db-er-diagram.mmd).

The database holds only the domain tables plus schema_migrations and REVIEW_QUEUE;
pipeline mechanics live in JSON files under the environment data directory.
"""
import hashlib
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from . import config

MIGRATIONS = Path(__file__).with_name("migrations")
REQUIRED_MIGRATIONS = ("001_domain_schema", "002_review_queue", "003_gate_issue")


def _open(paths, mode):
    paths.validate()
    connection = sqlite3.connect(paths.database.as_uri() + f"?mode={mode}", uri=True, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA busy_timeout=30000")
    return connection


def _check_schema(connection, allow_empty=False):
    tables = {r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if not tables and allow_empty:
        return
    if "schema_migrations" not in tables:
        raise RuntimeError("目标不是新结构数据库（缺少 schema_migrations）；请先 db init。")
    applied = {row[0] for row in connection.execute("SELECT version FROM schema_migrations")}
    missing = set(REQUIRED_MIGRATIONS) - applied
    if missing:
        raise RuntimeError(f"数据库缺少迁移：{', '.join(sorted(missing))}；请先 db init。")


def initialize(paths=None):
    """Explicit, transactional, checksummed schema initialization; inserts no business data."""
    paths = paths or config.PATHS
    paths.validate()
    paths.data.mkdir(parents=True, exist_ok=True)
    connection = _open(paths, "rwc")
    try:
        connection.execute("BEGIN IMMEDIATE")
        tables = {r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if tables and "schema_migrations" not in tables:
            raise RuntimeError("目标库存在旧结构残留，拒绝原地升级；请归档后重新 db init。")
        connection.execute("""CREATE TABLE IF NOT EXISTS schema_migrations (
            version TEXT PRIMARY KEY, checksum TEXT NOT NULL, applied_at TEXT NOT NULL)""")
        files = sorted(MIGRATIONS.glob("[0-9]*.sql"))
        if not files:
            raise RuntimeError("缺少数据库结构迁移文件")
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat()
        applied = {row["version"]: row["checksum"] for row in connection.execute("SELECT * FROM schema_migrations")}
        if set(applied) - {file.stem for file in files}:
            raise RuntimeError("数据库包含当前代码未知的迁移，拒绝降级使用")
        for file in files:
            sql = file.read_text(encoding="utf-8")
            checksum = hashlib.sha256(sql.encode("utf-8")).hexdigest()
            if file.stem in applied:
                if applied[file.stem] != checksum:
                    raise RuntimeError(f"已应用的迁移被修改：{file.name}；必须新增迁移文件")
                continue
            # executescript would implicitly commit; execute complete statements individually.
            statement = ""
            for line in sql.splitlines(keepends=True):
                statement += line
                if sqlite3.complete_statement(statement):
                    connection.execute(statement)
                    statement = ""
            if statement.strip():
                raise RuntimeError(f"迁移末尾有不完整 SQL：{file.name}")
            connection.execute("INSERT INTO schema_migrations VALUES (?,?,?)", (file.stem, checksum, now))
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
    paths.snapshots.mkdir(parents=True, exist_ok=True)
    return status(paths)


@contextmanager
def session(paths=None, readonly=True):
    """No DDL on reads. New services must explicitly request a write transaction."""
    paths = paths or config.PATHS
    connection = _open(paths, "ro" if readonly else "rw")
    try:
        _check_schema(connection)
        if readonly:
            connection.execute("PRAGMA query_only=ON")
        else:
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


def status(paths=None):
    paths = paths or config.PATHS
    paths.validate()
    result = {"environment": paths.environment, "database": str(paths.database),
              "initialized": False}
    if not paths.database.exists():
        return result
    with session(paths) as connection:
        result["initialized"] = True
        result["migrations"] = [dict(row) for row in connection.execute("SELECT * FROM schema_migrations ORDER BY version")]
        result["tables"] = [row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        result["open_reviews"] = connection.execute(
            "SELECT COUNT(*) FROM REVIEW_QUEUE WHERE status='OPEN'").fetchone()[0]
        result["open_gate_issues"] = connection.execute(
            "SELECT COUNT(*) FROM GATE_ISSUE WHERE status='OPEN'").fetchone()[0]
    return result
