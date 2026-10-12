"""Formal schema lifecycle. No legacy data is copied or implicitly migrated."""
import hashlib
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from . import config

MIGRATIONS = Path(__file__).with_name("migrations")


def _open(paths, mode):
    paths.validate()
    connection = sqlite3.connect(paths.database.as_uri() + f"?mode={mode}", uri=True, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA busy_timeout=30000")
    return connection


def _check_identity(connection, paths, allow_empty=False):
    tables = {r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if not tables and allow_empty:
        return
    if "database_identity" not in tables:
        raise RuntimeError("目标不是新结构数据库，禁止原地升级旧库；请先归档并按迁移方案处理。")
    row = connection.execute("SELECT environment, schema_family FROM database_identity WHERE id=1").fetchone()
    if not row or row["environment"] != paths.environment or row["schema_family"] != "agent1-formal":
        raise RuntimeError("数据库环境标记不匹配，拒绝跨环境使用或复制后直接使用。")


def initialize(paths=None):
    """Explicit, transactional, checksummed schema initialization; inserts no business data."""
    paths = paths or config.PATHS
    paths.validate()
    paths.data.mkdir(parents=True, exist_ok=True)
    connection = _open(paths, "rwc")
    try:
        connection.execute("BEGIN IMMEDIATE")
        _check_identity(connection, paths, allow_empty=True)
        connection.execute("""CREATE TABLE IF NOT EXISTS database_identity (
            id INTEGER PRIMARY KEY CHECK(id=1), environment TEXT NOT NULL
            CHECK(environment IN ('development','production','test')),
            schema_family TEXT NOT NULL CHECK(schema_family='agent1-formal'), created_at TEXT NOT NULL)""")
        connection.execute("""CREATE TABLE IF NOT EXISTS schema_migrations (
            version TEXT PRIMARY KEY, checksum TEXT NOT NULL, applied_at TEXT NOT NULL)""")
        now = datetime.now(timezone.utc).isoformat()
        connection.execute("INSERT OR IGNORE INTO database_identity VALUES (1,?,'agent1-formal',?)",
                           (paths.environment, now))
        files = sorted(MIGRATIONS.glob("[0-9]*.sql"))
        if not files:
            raise RuntimeError("缺少数据库结构迁移文件")
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
        _check_identity(connection, paths)
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
              "initialized": False, "legacy_mixed_database": str(config.LEGACY_MIXED_DB)}
    if not paths.database.exists():
        return result
    with session(paths) as connection:
        result["initialized"] = True
        result["migrations"] = [dict(row) for row in connection.execute("SELECT * FROM schema_migrations ORDER BY version")]
        result["tables"] = [row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
    return result
