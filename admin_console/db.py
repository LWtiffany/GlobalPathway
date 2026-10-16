"""PostgreSQL connection and idempotent schema bootstrap for the admin console.

Translates Agent1's SQLite domain schema into PostgreSQL (the only differences:
``GLOB`` -> regex, ``INSERT OR IGNORE/REPLACE`` -> ``ON CONFLICT``, ``ATTACH`` ->
``admin`` schema). All statements are idempotent, so ``connect_and_prepare`` is
safe to call on every startup.
"""
import psycopg
from psycopg.rows import dict_row

from . import config

# Domain tables (public schema) + admin schema (admin_user / review_queue).
SCHEMA = """
CREATE SCHEMA IF NOT EXISTS admin;

CREATE TABLE IF NOT EXISTS DOCUMENT (
    doc_id   TEXT PRIMARY KEY,
    code     TEXT NOT NULL,
    name_zh  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS VISA_POLICY (
    policy_id                TEXT PRIMARY KEY,
    country                  TEXT    NOT NULL,
    policy_type              TEXT    NOT NULL,
    title                    TEXT    NOT NULL,
    effective_from           DATE,
    effective_to             DATE,
    cas_required             INTEGER CHECK (cas_required IN (0,1)),
    i20_required             INTEGER CHECK (i20_required IN (0,1)),
    funds_tuition_unpaid     NUMERIC,
    funds_tuition_currency   TEXT    CHECK (funds_tuition_currency IS NULL OR funds_tuition_currency ~ '^[A-Z]{3}$'),
    funds_living_monthly     NUMERIC,
    funds_living_currency    TEXT    CHECK (funds_living_currency  IS NULL OR funds_living_currency  ~ '^[A-Z]{3}$'),
    funds_living_months      INTEGER,
    funds_holding_days       INTEGER,
    funds_doc_max_age_days   INTEGER,
    funds_owner_allowed      TEXT,
    english_level            TEXT,
    tb_test_required         INTEGER CHECK (tb_test_required IN (0,1)),
    health_surcharge         NUMERIC,
    apply_window_days        INTEGER,
    processing_days          INTEGER,
    visa_fee                 NUMERIC,
    applies_to_city          TEXT,
    remark                   TEXT,
    CHECK (funds_tuition_unpaid IS NULL OR funds_tuition_currency IS NOT NULL),
    CHECK (funds_living_monthly  IS NULL OR funds_living_currency  IS NOT NULL),
    CHECK (effective_from IS NULL OR effective_to IS NULL OR effective_from <= effective_to)
);

CREATE TABLE IF NOT EXISTS FX_POLICY (
    policy_id                TEXT PRIMARY KEY,
    country                  TEXT    NOT NULL,
    policy_type              TEXT    NOT NULL,
    title                    TEXT    NOT NULL,
    effective_from           DATE,
    effective_to             DATE,
    annual_quota_amount      NUMERIC,
    annual_quota_currency    TEXT    CHECK (annual_quota_currency IS NULL OR annual_quota_currency ~ '^[A-Z]{3}$'),
    quota_reset              TEXT,
    over_quota_allowed       INTEGER CHECK (over_quota_allowed IN (0,1)),
    purpose_code             TEXT,
    tuition_direct_to_school INTEGER CHECK (tuition_direct_to_school IN (0,1)),
    remitter_relation        TEXT,
    can_use_family_quota     INTEGER CHECK (can_use_family_quota IN (0,1)),
    remark                   TEXT,
    CHECK (annual_quota_amount IS NULL OR annual_quota_currency IS NOT NULL),
    CHECK (effective_from IS NULL OR effective_to IS NULL OR effective_from <= effective_to)
);

CREATE TABLE IF NOT EXISTS POLICY_DOCUMENT (
    policy_id      TEXT NOT NULL,
    policy_domain  TEXT NOT NULL CHECK (policy_domain IN ('visa_policy','fx_policy')),
    doc_id         TEXT NOT NULL,
    required       INTEGER CHECK (required IS NULL OR required IN (0,1)),
    "condition"    TEXT,
    CHECK (required IS NOT NULL OR "condition" IS NULL),
    PRIMARY KEY (policy_id, policy_domain, doc_id)
);

CREATE TABLE IF NOT EXISTS SCHOOL (
    school_id   TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    country     TEXT,
    city        TEXT
);

CREATE TABLE IF NOT EXISTS PROGRAM (
    program_id   TEXT PRIMARY KEY,
    school_id    TEXT NOT NULL,
    name         TEXT NOT NULL,
    level        TEXT,
    scope        TEXT CHECK (scope IS NULL OR scope IN ('programme','division','school_wide'))
);

CREATE TABLE IF NOT EXISTS TUITION (
    tuition_id     TEXT PRIMARY KEY,
    program_id     TEXT NOT NULL,
    academic_year  TEXT,
    amount         NUMERIC,
    currency       TEXT CHECK (currency IS NULL OR currency ~ '^[A-Z]{3}$'),
    fee_status     TEXT CHECK (fee_status IS NULL OR fee_status IN
                       ('home','international','local','non_local','in_state','out_of_state','all')),
    fee_basis      TEXT CHECK (fee_basis IS NULL OR fee_basis IN ('per_year','total','per_credit','per_term')),
    study_mode     TEXT CHECK (study_mode IS NULL OR study_mode IN ('full_time','part_time','any')),
    rate_label     TEXT,
    remark         TEXT
);

CREATE TABLE IF NOT EXISTS POLICY_EVIDENCE (
    evidence_id  TEXT PRIMARY KEY,
    policy_id    TEXT NOT NULL,
    target       TEXT NOT NULL CHECK (target LIKE 'field:%' OR target LIKE 'document:%'),
    kind         TEXT NOT NULL CHECK (kind IN ('value','condition')),
    source_id    TEXT NOT NULL,
    snapshot_id  TEXT NOT NULL,
    quote        TEXT NOT NULL,
    recorded_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_policy_evidence_target ON POLICY_EVIDENCE(policy_id, target);

CREATE TABLE IF NOT EXISTS FX_EVIDENCE (
    evidence_id  TEXT PRIMARY KEY,
    policy_id    TEXT NOT NULL,
    target       TEXT NOT NULL CHECK (target LIKE 'field:%' OR target LIKE 'document:%'),
    kind         TEXT NOT NULL CHECK (kind IN ('value','condition')),
    source_id    TEXT NOT NULL,
    snapshot_id  TEXT NOT NULL,
    quote        TEXT NOT NULL,
    recorded_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_fx_evidence_target ON FX_EVIDENCE(policy_id, target);

CREATE TABLE IF NOT EXISTS TUITION_EVIDENCE (
    evidence_id  TEXT PRIMARY KEY,
    tuition_id   TEXT NOT NULL,
    kind         TEXT NOT NULL CHECK (kind IN ('amount','context')),
    source_id    TEXT NOT NULL,
    snapshot_id  TEXT NOT NULL,
    quote        TEXT NOT NULL,
    recorded_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tuition_evidence ON TUITION_EVIDENCE(tuition_id);

CREATE TABLE IF NOT EXISTS admin.admin_user (
    username        TEXT PRIMARY KEY CHECK (length(username) BETWEEN 1 AND 40),
    password_hash   TEXT NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL,
    last_login_at   TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS admin.review_queue (
    review_id       TEXT PRIMARY KEY,
    domain_table    TEXT NOT NULL CHECK (domain_table IN ('VISA_POLICY','FX_POLICY','ACCOUNT_THRESHOLD','CREDIT_CARD','SCHOOL','PROGRAM','TUITION')),
    record_key      TEXT NOT NULL,
    action          TEXT NOT NULL CHECK (action IN ('CREATE','UPDATE')),
    field_path      TEXT,
    proposed_value  TEXT,
    evidence_json   TEXT,
    reason          TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    UNIQUE (domain_table, record_key, action)
);
"""


def connect():
    """Open a connection to the shared PostgreSQL database via the tunnel."""
    return psycopg.connect(
        host=config.DB_HOST, port=config.DB_PORT, dbname=config.DB_NAME,
        user=config.DB_USER, password=config.DB_PASSWORD,
        row_factory=dict_row, connect_timeout=10,
    )


def connect_and_prepare():
    """Connect and ensure the schema exists (idempotent)."""
    connection = connect()
    connection.execute(SCHEMA)
    connection.commit()
    return connection
