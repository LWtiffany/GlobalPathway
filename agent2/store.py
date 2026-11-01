"""Agent2 customer-state storage on PostgreSQL RDS.

Identity, Profile, conversations, messages, and current advice live in the
existing ``userinformation`` schema.  The dedicated ``agent2`` schema contains
only audit/lifecycle extensions.  Product and knowledge tables remain in
``public``.  There is intentionally no SQLite runtime fallback.
"""
import json
import threading
import uuid
from contextlib import contextmanager

import psycopg
from psycopg import sql
from psycopg.rows import dict_row

from . import config

SESSIONS_TABLE_DDL = """CREATE TABLE IF NOT EXISTS sessions (
    session_id      TEXT PRIMARY KEY,
    user_id         TEXT NOT NULL REFERENCES users(user_id),
    profile_id      TEXT NOT NULL UNIQUE REFERENCES profiles(profile_id),
    profile_version INTEGER NOT NULL,
    created_at      TEXT NOT NULL
);"""

SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version    TEXT PRIMARY KEY,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS users (
    user_id          TEXT PRIMARY KEY,
    external_subject TEXT NOT NULL UNIQUE,
    display_name     TEXT NOT NULL,
    synthetic        INTEGER NOT NULL CHECK (synthetic IN (0,1)),
    status           TEXT NOT NULL DEFAULT 'active'
);
CREATE TABLE IF NOT EXISTS auth_sessions (
    token_hash TEXT PRIMARY KEY,
    user_id    TEXT NOT NULL REFERENCES users(user_id),
    csrf_token TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS profiles (
    profile_id TEXT PRIMARY KEY,
    user_id    TEXT NOT NULL REFERENCES users(user_id),
    display_name TEXT,
    version    INTEGER NOT NULL,
    status     TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','archived')),
    synthetic  INTEGER NOT NULL CHECK (synthetic IN (0,1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    destination TEXT,
    course TEXT,
    offer_status TEXT,
    enrollment_date DATE,
    visa_category TEXT,
    school TEXT,
    profile_data_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    remittance_advice_json JSONB,
    account_advice_json JSONB,
    visa_advice_json JSONB,
    card_advice_json JSONB
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
    storage_class TEXT NOT NULL DEFAULT 'core_profile',
    confirmation_mode TEXT NOT NULL DEFAULT 'direct_answer',
    scope TEXT NOT NULL DEFAULT 'global',
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
CREATE TABLE IF NOT EXISTS bank_relationships (
    relationship_id TEXT PRIMARY KEY,
    profile_id TEXT NOT NULL REFERENCES profiles(profile_id) ON DELETE CASCADE,
    institution TEXT NOT NULL,
    market TEXT NOT NULL,
    product_id TEXT,
    product_name_snapshot TEXT NOT NULL,
    holder_relation TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('selected','applying','open','blocked','closed')),
    opened_on TEXT,
    source_message_id TEXT,
    confirmed_at TEXT NOT NULL,
    last_confirmed_at TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_bank_relationships_profile_market
    ON bank_relationships(profile_id, market, status);
CREATE TABLE IF NOT EXISTS bank_relationship_events (
    event_id TEXT PRIMARY KEY,
    relationship_id TEXT NOT NULL REFERENCES bank_relationships(relationship_id) ON DELETE CASCADE,
    old_status TEXT,
    new_status TEXT NOT NULL,
    source_message_id TEXT,
    actor_type TEXT NOT NULL CHECK (actor_type IN ('user','agent','system')),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS bank_relationship_proposals (
    proposal_id TEXT PRIMARY KEY,
    profile_id TEXT NOT NULL REFERENCES profiles(profile_id) ON DELETE CASCADE,
    market TEXT NOT NULL,
    product_id TEXT,
    product_name_snapshot TEXT NOT NULL,
    holder_relation TEXT NOT NULL,
    proposed_status TEXT NOT NULL CHECK (proposed_status IN ('selected','applying','open','blocked','closed')),
    existing_relationship_id TEXT REFERENCES bank_relationships(relationship_id),
    source_message_id TEXT,
    basis TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('open','confirmed','dismissed')),
    created_at TEXT NOT NULL,
    decided_at TEXT
);
CREATE TABLE IF NOT EXISTS sessions (
    session_id      TEXT PRIMARY KEY,
    user_id         TEXT NOT NULL REFERENCES users(user_id),
    profile_id      TEXT NOT NULL UNIQUE REFERENCES profiles(profile_id),
    profile_version INTEGER NOT NULL,
    created_at      TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_sessions_profile ON sessions(profile_id);
CREATE TABLE IF NOT EXISTS domain_conversation_state (
    session_id TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
    domain     TEXT NOT NULL,
    version    INTEGER NOT NULL,
    state_json TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (session_id, domain)
);
CREATE TABLE IF NOT EXISTS message_index (
    message_id        TEXT PRIMARY KEY,
    conversation_id   TEXT NOT NULL REFERENCES sessions(session_id),
    seq               INTEGER NOT NULL,
    role              TEXT NOT NULL CHECK (role IN ('user','assistant')),
    client_message_id TEXT,
    reply_to          TEXT,
    body_json         TEXT NOT NULL,
    status            TEXT NOT NULL,
    created_at        TEXT NOT NULL,
    UNIQUE (conversation_id, seq),
    UNIQUE (conversation_id, role, client_message_id)
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
    profile_version   INTEGER NOT NULL DEFAULT 0,
    dependency_snapshot_json TEXT NOT NULL DEFAULT '{}',
    regeneration_recipe_json TEXT NOT NULL DEFAULT '{}',
    stale_reason_json TEXT,
    schema_version    INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (profile_id, topic)
);
CREATE TABLE IF NOT EXISTS advice_history (
    profile_id        TEXT NOT NULL,
    topic             TEXT NOT NULL,
    version           INTEGER NOT NULL,
    payload_json      TEXT NOT NULL,
    source_message_id TEXT NOT NULL,
    created_at        TEXT NOT NULL,
    profile_version   INTEGER NOT NULL DEFAULT 0,
    dependency_snapshot_json TEXT NOT NULL DEFAULT '{}',
    regeneration_recipe_json TEXT NOT NULL DEFAULT '{}',
    schema_version    INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (profile_id, topic, version)
);
CREATE TABLE IF NOT EXISTS advice_scoped_current (
    profile_id TEXT NOT NULL REFERENCES profiles(profile_id),
    topic TEXT NOT NULL,
    scope_key TEXT NOT NULL,
    version INTEGER NOT NULL,
    source_message_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    status TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (profile_id, topic, scope_key)
);
CREATE TABLE IF NOT EXISTS advice_refresh_jobs (
    job_id TEXT PRIMARY KEY,
    profile_id TEXT NOT NULL REFERENCES profiles(profile_id) ON DELETE CASCADE,
    topic TEXT NOT NULL CHECK (topic IN ('remittance','account','visa','credit_card')),
    requested_profile_version INTEGER NOT NULL,
    source_advice_version INTEGER NOT NULL,
    regeneration_recipe_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN
        ('queued','running','needs_input','failed','succeeded','superseded')),
    idempotency_key TEXT NOT NULL UNIQUE,
    error_code TEXT,
    error_message TEXT,
    result_advice_version INTEGER,
    created_at TEXT NOT NULL,
    started_at TEXT,
    completed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_advice_refresh_jobs_profile_status
    ON advice_refresh_jobs(profile_id, status, created_at);
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

MIGRATIONS = (
    ("20261031_001_advice_lifecycle", """
        ALTER TABLE advice_current ADD COLUMN IF NOT EXISTS profile_version INTEGER NOT NULL DEFAULT 0;
        ALTER TABLE advice_current ADD COLUMN IF NOT EXISTS dependency_snapshot_json TEXT NOT NULL DEFAULT '{}';
        ALTER TABLE advice_current ADD COLUMN IF NOT EXISTS regeneration_recipe_json TEXT NOT NULL DEFAULT '{}';
        ALTER TABLE advice_current ADD COLUMN IF NOT EXISTS stale_reason_json TEXT;
        ALTER TABLE advice_current ADD COLUMN IF NOT EXISTS schema_version INTEGER NOT NULL DEFAULT 1;
        ALTER TABLE advice_history ADD COLUMN IF NOT EXISTS profile_version INTEGER NOT NULL DEFAULT 0;
        ALTER TABLE advice_history ADD COLUMN IF NOT EXISTS dependency_snapshot_json TEXT NOT NULL DEFAULT '{}';
        ALTER TABLE advice_history ADD COLUMN IF NOT EXISTS regeneration_recipe_json TEXT NOT NULL DEFAULT '{}';
        ALTER TABLE advice_history ADD COLUMN IF NOT EXISTS schema_version INTEGER NOT NULL DEFAULT 1;
    """),
)


def dumps(value):
    return json.dumps(value, ensure_ascii=False, default=str)


def _postgres_sql(statement):
    """Translate the service's DB-API qmark placeholders to psycopg format.

    The existing service SQL never uses PostgreSQL's JSON ``?`` operator.  A
    small quote-aware translator keeps the storage migration contained instead
    of mixing dialect concerns through every business method.
    """
    result, quote, index = [], None, 0
    while index < len(statement):
        char = statement[index]
        if quote:
            result.append(char)
            if char == quote:
                if index + 1 < len(statement) and statement[index + 1] == quote:
                    result.append(statement[index + 1])
                    index += 1
                else:
                    quote = None
        elif char in {"'", '"'}:
            quote = char
            result.append(char)
        elif char == "?":
            result.append("%s")
        else:
            result.append(char)
        index += 1
    return "".join(result)


class _ConnectionAdapter:
    def __init__(self, connection):
        self.connection = connection

    def execute(self, statement, params=()):
        return self.connection.execute(_postgres_sql(statement), params)

    def executemany(self, statement, params_seq):
        cursor = self.connection.cursor()
        cursor.executemany(_postgres_sql(statement), params_seq)
        return cursor


class Store:
    """PostgreSQL-backed Agent2 state store.

    ``temporary()`` creates an isolated RDS schema for integration tests and
    drops only that verified schema on close.  Normal construction always uses
    the configured durable schema.
    """

    def __init__(self, *, schema=None, temporary=False):
        self.schema = schema or config.DB_SCHEMA
        self._temporary = temporary
        self.user_schema = self.schema if temporary else config.USER_SCHEMA
        self._lock = threading.RLock()
        self._connection = psycopg.connect(
            **config.connection_kwargs(), row_factory=dict_row, autocommit=True)
        self._prepare()

    @classmethod
    def temporary(cls):
        return cls(schema=f"agent2_test_{uuid.uuid4().hex}", temporary=True)

    def _prepare(self):
        if not self.schema or not self.schema.replace("_", "").isalnum():
            raise ValueError("非法 PostgreSQL schema 名称")
        with self._lock:
            self._connection.execute(
                sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(sql.Identifier(self.schema)))
            self._connection.execute(
                sql.SQL("SET search_path TO {}, {}, public").format(
                    # Canonical identity/Profile tables must win name resolution.
                    # Agent2 contains workflow extensions only.
                    sql.Identifier(self.user_schema), sql.Identifier(self.schema)))
            schema_ddl = SCHEMA
            if not self._temporary:
                # The durable userinformation schema keeps conversations in a
                # table named ``conversations`` and exposes it to the service's
                # legacy ``sessions`` SQL through a view.  Creating the SQLite-era
                # sessions TABLE here would shadow that view with an empty table.
                schema_ddl = schema_ddl.replace(SESSIONS_TABLE_DDL, "")
                schema_ddl = schema_ddl.replace(
                    "CREATE UNIQUE INDEX IF NOT EXISTS uq_sessions_profile ON sessions(profile_id);", "")
            self._connection.execute(schema_ddl)
            for version, statement in MIGRATIONS:
                applied = self._connection.execute(
                    "SELECT 1 FROM schema_migrations WHERE version=%s", (version,)).fetchone()
                if applied:
                    continue
                with self._connection.transaction():
                    self._connection.execute(statement)
                    self._connection.execute(
                        "INSERT INTO schema_migrations(version) VALUES (%s) ON CONFLICT DO NOTHING",
                        (version,))

    def canonical_table(self, name):
        if name not in {"users", "profiles", "conversations", "message_index"}:
            raise ValueError("未知 canonical table")
        return f'"{self.user_schema}"."{name}"'

    @property
    def profile_name_column(self):
        # Isolated test schemas use the compact compatibility DDL above; the
        # durable userinformation schema calls this first-class column student_name.
        return "display_name" if self._temporary else "student_name"

    @contextmanager
    def transaction(self):
        with self._lock:
            with self._connection.transaction():
                yield _ConnectionAdapter(self._connection)

    def close(self):
        with self._lock:
            if self._connection.closed:
                return
            if self._temporary:
                prefix = "agent2_test_"
                if not self.schema.startswith(prefix) or len(self.schema) <= len(prefix):
                    raise RuntimeError("拒绝删除未验证的测试 schema")
                self._connection.execute(
                    sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(self.schema)))
            self._connection.close()

    def one(self, statement, params=()):
        with self._lock:
            row = self._connection.execute(_postgres_sql(statement), params).fetchone()
        return dict(row) if row else None

    def all(self, statement, params=()):
        with self._lock:
            rows = self._connection.execute(_postgres_sql(statement), params).fetchall()
        return [dict(row) for row in rows]
