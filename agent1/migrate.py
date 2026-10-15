"""Checksummed SQL migrations shared by agent1.sqlite3 and admin.sqlite3."""
import hashlib
import sqlite3
from datetime import datetime, timezone


def apply(connection, directory, schema="main"):
    """Apply every `directory/[0-9]*.sql` not yet recorded in `schema.schema_migrations`.

    Runs inside the caller's transaction; never commits."""
    connection.execute(f"""CREATE TABLE IF NOT EXISTS {schema}.schema_migrations (
        version TEXT PRIMARY KEY, checksum TEXT NOT NULL, applied_at TEXT NOT NULL)""")
    files = sorted(directory.glob("[0-9]*.sql"))
    if not files:
        raise RuntimeError(f"缺少数据库结构迁移文件：{directory}")
    now = datetime.now(timezone.utc).isoformat()
    applied = dict(connection.execute(f"SELECT version, checksum FROM {schema}.schema_migrations").fetchall())
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
        connection.execute(f"INSERT INTO {schema}.schema_migrations VALUES (?,?,?)", (file.stem, checksum, now))


def missing(connection, required, schema="main"):
    tables = {r[0] for r in connection.execute(f"SELECT name FROM {schema}.sqlite_master WHERE type='table'")}
    if "schema_migrations" not in tables:
        return set(required)
    applied = {row[0] for row in connection.execute(f"SELECT version FROM {schema}.schema_migrations")}
    return set(required) - applied
