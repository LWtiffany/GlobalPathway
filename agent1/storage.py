"""PostgreSQL storage for the formal domain schema (db-er-diagram.mmd).

权威存储从 SQLite 迁移到阿里云 RDS PostgreSQL。表结构与 admin_console 已建
的 PG schema 保持一致（域表在 public schema，审核队列在 admin schema）；agent1
独有的发布/门控/审计表用 PG 方言补齐。所有 DDL 幂等，可安全重复执行。
"""
from contextlib import contextmanager

import psycopg
from psycopg.rows import dict_row

from . import config

# 域表（public schema，与 admin_console 一致）+ admin schema（admin_user /
# review_queue）+ agent1 独有表（GATE_ISSUE / 发布面 / 账户与信用卡域）。
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

CREATE TABLE IF NOT EXISTS ACCOUNT_THRESHOLD (
    threshold_id          TEXT PRIMARY KEY,
    bank_code             TEXT    NOT NULL,
    product_code          TEXT    NOT NULL,
    country               TEXT    NOT NULL,
    min_age               INTEGER,
    min_deposit           NUMERIC,
    min_deposit_currency  TEXT    CHECK (min_deposit_currency IS NULL OR min_deposit_currency ~ '^[A-Z]{3}$'),
    residency_note        TEXT,
    CHECK (min_deposit IS NULL OR min_deposit_currency IS NOT NULL)
);

CREATE TABLE IF NOT EXISTS ACCOUNT_DOC (
    threshold_id   TEXT NOT NULL,
    doc_id         TEXT NOT NULL,
    required       INTEGER NOT NULL CHECK (required IN (0,1)),
    PRIMARY KEY (threshold_id, doc_id)
);

CREATE TABLE IF NOT EXISTS CREDIT_CARD (
    card_id           TEXT PRIMARY KEY,
    issuing_bank      TEXT NOT NULL,
    card_network      TEXT NOT NULL,
    market            TEXT NOT NULL,
    product_name      TEXT NOT NULL,
    card_tier         TEXT,
    product_status    TEXT NOT NULL DEFAULT 'ACTIVE'
        CHECK (product_status IN ('ACTIVE','SUSPENDED','DISCONTINUED')),
    target_audience   TEXT,
    official_url      TEXT,
    data_status       TEXT NOT NULL DEFAULT 'synthetic'
        CHECK (data_status IN ('verified','estimated','synthetic')),
    effective_from    DATE,
    effective_to      DATE,
    last_verified_at  TIMESTAMPTZ,
    UNIQUE (issuing_bank, market, product_name),
    CHECK (effective_to IS NULL OR effective_from IS NULL OR effective_to >= effective_from)
);

CREATE TABLE IF NOT EXISTS CARD_REWARD_RULE (
    reward_rule_id           TEXT PRIMARY KEY,
    card_id                  TEXT NOT NULL REFERENCES CREDIT_CARD(card_id) ON DELETE CASCADE,
    spend_category           TEXT NOT NULL,
    reward_type              TEXT NOT NULL CHECK (reward_type IN ('cashback','points','miles')),
    reward_rate              NUMERIC CHECK (reward_rate IS NULL OR reward_rate BETWEEN 0 AND 1),
    points_per_currency_unit NUMERIC CHECK (points_per_currency_unit IS NULL OR points_per_currency_unit >= 0),
    spend_cap                NUMERIC CHECK (spend_cap IS NULL OR spend_cap >= 0),
    cap_currency             TEXT CHECK (cap_currency IS NULL OR cap_currency ~ '^[A-Z]{3}$'),
    cap_period               TEXT CHECK (cap_period IS NULL OR cap_period IN ('transaction','month','quarter','year')),
    description              TEXT,
    CHECK (reward_rate IS NOT NULL OR points_per_currency_unit IS NOT NULL),
    CHECK (spend_cap IS NULL OR cap_currency IS NOT NULL)
);
CREATE INDEX IF NOT EXISTS idx_card_reward_rule_card ON CARD_REWARD_RULE(card_id);

CREATE TABLE IF NOT EXISTS CARD_FEE (
    fee_id            TEXT PRIMARY KEY,
    card_id           TEXT NOT NULL REFERENCES CREDIT_CARD(card_id) ON DELETE CASCADE,
    fee_type          TEXT NOT NULL CHECK (fee_type IN ('annual_fee','foreign_transaction_fee','cash_advance_fee','late_payment_fee','other')),
    amount            NUMERIC CHECK (amount IS NULL OR amount >= 0),
    currency          TEXT CHECK (currency IS NULL OR currency ~ '^[A-Z]{3}$'),
    rate              NUMERIC CHECK (rate IS NULL OR rate BETWEEN 0 AND 1),
    waiver_condition  TEXT,
    notes             TEXT,
    CHECK (amount IS NOT NULL OR rate IS NOT NULL),
    CHECK (amount IS NULL OR currency IS NOT NULL)
);
CREATE INDEX IF NOT EXISTS idx_card_fee_card ON CARD_FEE(card_id);

CREATE TABLE IF NOT EXISTS CARD_BENEFIT (
    benefit_id    TEXT PRIMARY KEY,
    card_id       TEXT NOT NULL REFERENCES CREDIT_CARD(card_id) ON DELETE CASCADE,
    benefit_type  TEXT NOT NULL CHECK (benefit_type IN ('airport_lounge','travel_insurance','concierge','merchant_offer','digital_wallet','dual_currency','other')),
    benefit_name  TEXT NOT NULL,
    quantity      NUMERIC CHECK (quantity IS NULL OR quantity >= 0),
    period        TEXT CHECK (period IS NULL OR period IN ('transaction','month','quarter','year')),
    description   TEXT
);
CREATE INDEX IF NOT EXISTS idx_card_benefit_card ON CARD_BENEFIT(card_id);

CREATE TABLE IF NOT EXISTS CARD_ELIGIBILITY_RULE (
    eligibility_rule_id  TEXT PRIMARY KEY,
    card_id              TEXT NOT NULL REFERENCES CREDIT_CARD(card_id) ON DELETE CASCADE,
    rule_type            TEXT NOT NULL CHECK (rule_type IN ('minimum_age','minimum_income','student_status','banking_relationship','credit_assessment','residency','other')),
    operator             TEXT NOT NULL CHECK (operator IN ('eq','gte','lte','in','required')),
    value_text           TEXT,
    value_numeric        NUMERIC,
    currency             TEXT CHECK (currency IS NULL OR currency ~ '^[A-Z]{3}$'),
    required             INTEGER NOT NULL DEFAULT 1 CHECK (required IN (0,1)),
    description          TEXT,
    CHECK (value_text IS NOT NULL OR value_numeric IS NOT NULL),
    CHECK (currency IS NULL OR value_numeric IS NOT NULL)
);
CREATE INDEX IF NOT EXISTS idx_card_eligibility_card ON CARD_ELIGIBILITY_RULE(card_id);

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

CREATE TABLE IF NOT EXISTS GATE_ISSUE (
    issue_id         TEXT PRIMARY KEY,
    source_id        TEXT NOT NULL,
    snapshot_id      TEXT,
    scope            TEXT,
    check_name       TEXT NOT NULL,
    severity         TEXT NOT NULL CHECK (severity IN ('error','warning')),
    field            TEXT,
    value            TEXT,
    quote            TEXT,
    reason           TEXT NOT NULL,
    suggested_action TEXT,
    issue_key        TEXT NOT NULL,
    status           TEXT NOT NULL DEFAULT 'OPEN' CHECK (status IN ('OPEN','RESOLVED','IGNORED')),
    first_seen_at    TEXT NOT NULL,
    last_seen_at     TEXT NOT NULL,
    resolved_at      TEXT
);
CREATE INDEX IF NOT EXISTS idx_gate_issue_source_status ON GATE_ISSUE(source_id, status);
CREATE INDEX IF NOT EXISTS idx_gate_issue_key ON GATE_ISSUE(source_id, issue_key);

CREATE TABLE IF NOT EXISTS POLICY_REVISION (
    revision_id   TEXT PRIMARY KEY,
    policy_id     TEXT NOT NULL,
    revision_no   INTEGER NOT NULL CHECK (revision_no >= 1),
    facts_hash    TEXT NOT NULL,
    payload_json  TEXT NOT NULL,
    created_by    TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    UNIQUE (policy_id, revision_no)
);

CREATE TABLE IF NOT EXISTS POLICY_PUBLICATION (
    policy_id        TEXT PRIMARY KEY,
    revision_id      TEXT NOT NULL,
    status           TEXT NOT NULL CHECK (status IN ('ACTIVE','WITHDRAWN')),
    published_by     TEXT NOT NULL,
    published_at     TEXT NOT NULL,
    verified_at      TEXT NOT NULL,
    withdrawn_by     TEXT,
    withdrawn_at     TEXT,
    withdraw_reason  TEXT,
    CHECK (status = 'ACTIVE' OR (withdrawn_by IS NOT NULL AND withdrawn_at IS NOT NULL
                                 AND withdraw_reason IS NOT NULL))
);

CREATE TABLE IF NOT EXISTS PUBLICATION_EVENT (
    event_id              TEXT PRIMARY KEY,
    policy_id             TEXT NOT NULL,
    action                TEXT NOT NULL CHECK (action IN ('PUBLISH','REVERIFY','WITHDRAW')),
    revision_id           TEXT,
    previous_revision_id  TEXT,
    actor                 TEXT NOT NULL,
    reason                TEXT,
    detail_json           TEXT,
    at                    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_publication_event_policy ON PUBLICATION_EVENT(policy_id, at);

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

-- POLICY_REVISION 不可变、PUBLICATION_EVENT 只追加、发布必须指向同一 policy 的修订。
CREATE OR REPLACE FUNCTION _immutable_guard() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'this table is immutable';
END;
$$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS policy_revision_no_update ON POLICY_REVISION;
CREATE TRIGGER policy_revision_no_update BEFORE UPDATE ON POLICY_REVISION
    FOR EACH ROW EXECUTE FUNCTION _immutable_guard();
DROP TRIGGER IF EXISTS policy_revision_no_delete ON POLICY_REVISION;
CREATE TRIGGER policy_revision_no_delete BEFORE DELETE ON POLICY_REVISION
    FOR EACH ROW EXECUTE FUNCTION _immutable_guard();
DROP TRIGGER IF EXISTS publication_event_no_update ON PUBLICATION_EVENT;
CREATE TRIGGER publication_event_no_update BEFORE UPDATE ON PUBLICATION_EVENT
    FOR EACH ROW EXECUTE FUNCTION _immutable_guard();
DROP TRIGGER IF EXISTS publication_event_no_delete ON PUBLICATION_EVENT;
CREATE TRIGGER publication_event_no_delete BEFORE DELETE ON PUBLICATION_EVENT
    FOR EACH ROW EXECUTE FUNCTION _immutable_guard();

CREATE OR REPLACE FUNCTION _publication_own_revision() RETURNS trigger AS $$
DECLARE
    rev_policy TEXT;
BEGIN
    SELECT policy_id INTO rev_policy FROM POLICY_REVISION WHERE revision_id = NEW.revision_id;
    IF rev_policy IS DISTINCT FROM NEW.policy_id THEN
        RAISE EXCEPTION 'publication must point at a revision of the same policy';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS policy_publication_own_revision_insert ON POLICY_PUBLICATION;
CREATE TRIGGER policy_publication_own_revision_insert BEFORE INSERT ON POLICY_PUBLICATION
    FOR EACH ROW EXECUTE FUNCTION _publication_own_revision();
DROP TRIGGER IF EXISTS policy_publication_own_revision_update ON POLICY_PUBLICATION;
CREATE TRIGGER policy_publication_own_revision_update BEFORE UPDATE ON POLICY_PUBLICATION
    FOR EACH ROW EXECUTE FUNCTION _publication_own_revision();
"""


def _connect():
    """Open a connection to the shared PostgreSQL database."""
    return psycopg.connect(
        host=config.DB_HOST, port=config.DB_PORT, dbname=config.DB_NAME,
        user=config.DB_USER, password=config.DB_PASSWORD,
        row_factory=dict_row, connect_timeout=10,
    )


def _prepare(connection):
    """幂等建表（autocommit 下执行多语句 DDL）。"""
    connection.autocommit = True
    try:
        connection.execute(SCHEMA)
    finally:
        connection.autocommit = False


@contextmanager
def session(paths=None, readonly=True):
    """读写连接。readonly=True 只读事务；False 可写事务。

    paths 参数保留以兼容旧调用签名；数据库地址由 config 提供，与本地路径无关。
    """
    connection = _connect()
    try:
        _prepare(connection)
        connection.read_only = readonly
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


@contextmanager
def read_session():
    """Read an already-initialized RDS schema without running startup DDL."""
    connection = _connect()
    try:
        connection.read_only = True
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def initialize(paths=None):
    """幂等初始化 schema（建表 + 触发器），无业务数据写入。"""
    connection = _connect()
    try:
        _prepare(connection)
    finally:
        connection.close()
    return status(paths)


def status(paths=None):
    """返回连接与表清单（不依赖本地路径）。"""
    result = {
        "environment": config.ENVIRONMENT,
        "database": f"{config.DB_HOST}:{config.DB_PORT}/{config.DB_NAME}",
        "initialized": False,
    }
    try:
        with session(readonly=True) as connection:
            result["initialized"] = True
            result["tables"] = [r["tablename"] for r in connection.execute(
                "SELECT tablename FROM pg_tables "
                "WHERE schemaname IN ('public','admin') ORDER BY tablename")]
            result["open_reviews"] = connection.execute(
                "SELECT COUNT(*) AS n FROM admin.review_queue").fetchone()["n"]
            result["open_gate_issues"] = connection.execute(
                "SELECT COUNT(*) AS n FROM GATE_ISSUE WHERE status='OPEN'").fetchone()["n"]
            result["publications"] = dict(connection.execute(
                "SELECT status, COUNT(*) AS n FROM POLICY_PUBLICATION GROUP BY status").fetchall())
    except Exception as exc:  # noqa: BLE001 - status 是诊断接口，失败不抛出
        result["error"] = f"{type(exc).__name__}: {exc}"
    return result
