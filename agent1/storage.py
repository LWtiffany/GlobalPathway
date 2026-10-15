"""Formal schema lifecycle for the clean domain schema (db-er-diagram.mmd).

agent1.sqlite3 holds the domain tables, gate issues and publications; REVIEW_QUEUE
lives in admin.sqlite3, attached to every session as schema `admin` (admin_store).
Pipeline mechanics live in JSON files under the environment data directory.
"""
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from . import admin_store, config, migrate

MIGRATIONS = Path(__file__).with_name("migrations")
REQUIRED_MIGRATIONS = ("001_domain_schema", "002_review_queue", "003_gate_issue", "004_publication",
                       "005_fx_evidence", "006_tuition", "007_tuition_catalog", "008_tuition_rate",
                       "009_review_queue_to_admin")


def _open(paths, mode):
    paths.validate()
    connection = sqlite3.connect(paths.database.as_uri() + f"?mode={mode}", uri=True, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA busy_timeout=30000")
    return connection


def _check_schema(connection):
    tables = {r[0] for r in connection.execute("SELECT name FROM main.sqlite_master WHERE type='table'")}
    if "schema_migrations" not in tables:
        raise RuntimeError("目标不是新结构数据库（缺少 schema_migrations）；请先 db init。")
    lacking = migrate.missing(connection, REQUIRED_MIGRATIONS)
    if lacking:
        raise RuntimeError(f"数据库缺少迁移：{', '.join(sorted(lacking))}；请先 db init。")


def initialize(paths=None):
    """Explicit, transactional, checksummed schema initialization (agent1.sqlite3 and
    admin.sqlite3); inserts no business data."""
    paths = paths or config.PATHS
    paths.validate()
    paths.data.mkdir(parents=True, exist_ok=True)
    admin_store.initialize(paths)
    connection = _open(paths, "rwc")
    try:
        admin_store.attach(connection, paths, readonly=False)
        connection.execute("BEGIN IMMEDIATE")
        tables = {r[0] for r in connection.execute("SELECT name FROM main.sqlite_master WHERE type='table'")}
        if tables and "schema_migrations" not in tables:
            raise RuntimeError("目标库存在旧结构残留，拒绝原地升级；请归档后重新 db init。")
        migrate.apply(connection, MIGRATIONS)
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
        admin_store.attach(connection, paths, readonly)
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
            "SELECT name FROM main.sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        result["admin_database"] = str(paths.admin_database)
        result["open_reviews"] = connection.execute("SELECT COUNT(*) FROM admin.REVIEW_QUEUE").fetchone()[0]
        result["open_gate_issues"] = connection.execute(
            "SELECT COUNT(*) FROM GATE_ISSUE WHERE status='OPEN'").fetchone()[0]
        result["publications"] = dict(connection.execute(
            "SELECT status, COUNT(*) FROM POLICY_PUBLICATION GROUP BY status").fetchall())
    return result
