"""S1 本地存储适配器（SQLite）。

表结构按 docs/agent2-s1-uk-student-visa.md §6 的最小表集；目标环境按系统设计换 PostgreSQL + OSS，
需在目标存储上跑通同一套合同测试。本地适配器把消息正文存在 message_index.body_json。
"""
import json
import sqlite3
import threading
from contextlib import contextmanager

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    user_id          TEXT PRIMARY KEY,
    external_subject TEXT NOT NULL UNIQUE,
    display_name     TEXT NOT NULL,
    synthetic        INTEGER NOT NULL CHECK (synthetic IN (0,1)),
    status           TEXT NOT NULL DEFAULT 'active'
);
CREATE TABLE IF NOT EXISTS auth_sessions (
    token      TEXT PRIMARY KEY,
    user_id    TEXT NOT NULL REFERENCES users(user_id),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS profiles (
    profile_id TEXT PRIMARY KEY,
    user_id    TEXT NOT NULL REFERENCES users(user_id),
    display_name TEXT,
    version    INTEGER NOT NULL,
    status     TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','archived')),
    synthetic  INTEGER NOT NULL CHECK (synthetic IN (0,1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS profile_field_values (
    profile_id   TEXT NOT NULL REFERENCES profiles(profile_id) ON DELETE CASCADE,
    field_code   TEXT NOT NULL,
    value_json   TEXT,
    value_type   TEXT NOT NULL,
    status       TEXT NOT NULL CHECK (status IN
        ('unknown','inferred','proposed','confirmed','stale','disputed')),
    source_type  TEXT CHECK (source_type IS NULL OR source_type IN
        ('user_selection','user_message','user_edit','document_import','system_derived','agent_inference','demo')),
    confirmed_at TEXT,
    expires_at   TEXT,
    source_message_id TEXT,
    field_version INTEGER NOT NULL DEFAULT 0,
    sensitivity TEXT NOT NULL DEFAULT 'standard' CHECK (sensitivity IN
        ('standard','personal','financial','sensitive')),
    updated_at   TEXT NOT NULL,
    PRIMARY KEY (profile_id, field_code)
);
CREATE TABLE IF NOT EXISTS profile_field_definitions (
    field_code       TEXT PRIMARY KEY,
    display_name     TEXT NOT NULL,
    value_type       TEXT NOT NULL,
    options_json     TEXT NOT NULL,
    domains_json     TEXT NOT NULL,
    sensitivity      TEXT NOT NULL CHECK (sensitivity IN ('standard','personal','financial','sensitive')),
    freshness_days   INTEGER,
    ask_priority     INTEGER NOT NULL DEFAULT 0,
    ask_only_when_needed INTEGER NOT NULL DEFAULT 1 CHECK (ask_only_when_needed IN (0,1)),
    question_template TEXT NOT NULL,
    schema_version   INTEGER NOT NULL,
    active           INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0,1)),
    updated_at       TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS profile_field_events (
    event_id         TEXT PRIMARY KEY,
    profile_id       TEXT NOT NULL REFERENCES profiles(profile_id) ON DELETE CASCADE,
    field_code       TEXT NOT NULL,
    old_value_json   TEXT,
    new_value_json   TEXT,
    action           TEXT NOT NULL CHECK (action IN ('set','confirm','clear','expire','reject')),
    source_type      TEXT NOT NULL,
    source_message_id TEXT,
    actor_type       TEXT NOT NULL CHECK (actor_type IN ('user','agent','system')),
    profile_version  INTEGER NOT NULL,
    created_at       TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS profile_change_proposals (
    proposal_id       TEXT PRIMARY KEY,
    profile_id        TEXT NOT NULL REFERENCES profiles(profile_id) ON DELETE CASCADE,
    field_code        TEXT NOT NULL,
    old_value         TEXT,
    new_value         TEXT,
    source_message_id TEXT,
    basis             TEXT NOT NULL,
    status            TEXT NOT NULL CHECK (status IN ('open','confirmed','dismissed')),
    created_at        TEXT NOT NULL,
    decided_at        TEXT
);
CREATE TABLE IF NOT EXISTS sessions (
    session_id      TEXT PRIMARY KEY,
    user_id         TEXT NOT NULL REFERENCES users(user_id),
    profile_id      TEXT NOT NULL UNIQUE REFERENCES profiles(profile_id),
    profile_version INTEGER NOT NULL,
    created_at      TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_sessions_profile ON sessions(profile_id);
CREATE TABLE IF NOT EXISTS message_index (
    message_id        TEXT PRIMARY KEY,
    session_id        TEXT NOT NULL REFERENCES sessions(session_id),
    seq               INTEGER NOT NULL,
    role              TEXT NOT NULL CHECK (role IN ('user','assistant')),
    client_message_id TEXT,
    reply_to          TEXT,
    body_json         TEXT NOT NULL,
    status            TEXT NOT NULL,
    created_at        TEXT NOT NULL,
    UNIQUE (session_id, seq),
    UNIQUE (session_id, role, client_message_id)
);
CREATE TABLE IF NOT EXISTS answer_runs (
    run_id            TEXT PRIMARY KEY,
    message_id        TEXT NOT NULL UNIQUE REFERENCES message_index(message_id),
    profile_version   INTEGER NOT NULL,
    knowledge_read_id TEXT,
    model_version     TEXT NOT NULL,
    prompt_version    TEXT NOT NULL,
    gate_version      TEXT NOT NULL,
    answer_status     TEXT NOT NULL CHECK (answer_status IN
        ('answered','needs_input','needs_verification','cannot_answer','out_of_scope')),
    trace_id          TEXT NOT NULL,
    stage_ms_json     TEXT NOT NULL,
    created_at        TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS answer_citations (
    message_id  TEXT NOT NULL REFERENCES message_index(message_id),
    claim_id    TEXT NOT NULL,
    citation_id TEXT NOT NULL,
    record_id   TEXT NOT NULL,
    revision_id TEXT NOT NULL,
    evidence_id TEXT NOT NULL,
    source_url  TEXT,
    verified_at TEXT NOT NULL,
    PRIMARY KEY (message_id, claim_id, citation_id)
);
CREATE TABLE IF NOT EXISTS advice_current (
    profile_id        TEXT NOT NULL REFERENCES profiles(profile_id),
    topic             TEXT NOT NULL CHECK (topic IN ('remittance','account','visa','credit_card')),
    version           INTEGER NOT NULL,
    source_message_id TEXT NOT NULL,
    payload_json      TEXT NOT NULL,
    status            TEXT NOT NULL,
    updated_at        TEXT NOT NULL,
    PRIMARY KEY (profile_id, topic)
);
CREATE TABLE IF NOT EXISTS advice_history (
    profile_id        TEXT NOT NULL,
    topic             TEXT NOT NULL,
    version           INTEGER NOT NULL,
    payload_json      TEXT NOT NULL,
    source_message_id TEXT NOT NULL,
    created_at        TEXT NOT NULL,
    PRIMARY KEY (profile_id, topic, version)
);
CREATE TABLE IF NOT EXISTS message_reactions (
    message_id TEXT NOT NULL REFERENCES message_index(message_id),
    user_id    TEXT NOT NULL REFERENCES users(user_id),
    kind       TEXT NOT NULL CHECK (kind IN ('like','dislike')),
    created_at TEXT NOT NULL,
    PRIMARY KEY (message_id, user_id)
);
CREATE TABLE IF NOT EXISTS feedback_tickets (
    feedback_id TEXT PRIMARY KEY,
    message_id  TEXT NOT NULL REFERENCES message_index(message_id),
    user_id     TEXT NOT NULL REFERENCES users(user_id),
    note        TEXT NOT NULL,
    status      TEXT NOT NULL CHECK (status IN ('open','resolved')),
    created_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS plan_items (
    item_id    TEXT PRIMARY KEY,
    profile_id TEXT NOT NULL REFERENCES profiles(profile_id) ON DELETE CASCADE,
    text       TEXT NOT NULL,
    done       INTEGER NOT NULL CHECK (done IN (0,1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


def dumps(value):
    return json.dumps(value, ensure_ascii=False, default=str)


class Store:
    def __init__(self, path=":memory:"):
        self._connection = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA foreign_keys=ON")
        self._migrate_legacy_names()
        self._connection.executescript(SCHEMA)
        self._migrate_legacy_profile_fields()
        self._lock = threading.RLock()

    def _table_columns(self, table):
        return {row[1] for row in self._connection.execute(f"PRAGMA table_info({table})")}

    def _migrate_legacy_names(self):
        """Rename the original auth/conversation tables before creating the v2 schema."""
        tables = {row[0] for row in self._connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        if "profiles" in tables:
            columns = self._table_columns("profiles")
            if "display_name" not in columns:
                self._connection.execute("ALTER TABLE profiles ADD COLUMN display_name TEXT")
            if "status" not in columns:
                self._connection.execute(
                    "ALTER TABLE profiles ADD COLUMN status TEXT NOT NULL DEFAULT 'active'")
        if "sessions" in tables and "token" in self._table_columns("sessions"):
            if "auth_sessions" not in tables:
                self._connection.execute("ALTER TABLE sessions RENAME TO auth_sessions")
            else:
                self._connection.execute("DROP TABLE sessions")
            tables.discard("sessions")
            tables.add("auth_sessions")
        if "conversations" in tables and "sessions" not in tables:
            self._connection.execute("ALTER TABLE conversations RENAME TO sessions")
            if "conversation_id" in self._table_columns("sessions"):
                self._connection.execute("ALTER TABLE sessions RENAME COLUMN conversation_id TO session_id")
            if "message_index" in tables and "conversation_id" in self._table_columns("message_index"):
                self._connection.execute("ALTER TABLE message_index RENAME COLUMN conversation_id TO session_id")

    def _migrate_legacy_profile_fields(self):
        tables = {row[0] for row in self._connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        if "profile_fields" not in tables:
            return
        now = "1970-01-01T00:00:00+00:00"
        self._connection.execute("""INSERT OR IGNORE INTO profile_field_values
            (profile_id, field_code, value_json, value_type, status, source_type, confirmed_at,
             expires_at, source_message_id, field_version, sensitivity, updated_at)
            SELECT profile_id, field_code, value_json, 'json', status,
              CASE source WHEN 'user' THEN 'user_edit' WHEN 'import' THEN 'document_import'
                   WHEN 'demo' THEN 'demo' ELSE NULL END,
              confirmed_at, NULL, NULL, CASE WHEN status='confirmed' THEN 1 ELSE 0 END,
              'standard', COALESCE(confirmed_at, ?)
            FROM profile_fields""", (now,))

    @contextmanager
    def transaction(self):
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                yield self._connection
            except BaseException:
                self._connection.execute("ROLLBACK")
                raise
            self._connection.execute("COMMIT")

    def close(self):
        with self._lock:
            self._connection.close()

    def one(self, sql, params=()):
        with self._lock:
            row = self._connection.execute(sql, params).fetchone()
        return dict(row) if row else None

    def all(self, sql, params=()):
        with self._lock:
            return [dict(r) for r in self._connection.execute(sql, params).fetchall()]
