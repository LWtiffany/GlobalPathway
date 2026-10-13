-- 001_domain_schema.sql
-- The 10-table domain model from db-er-diagram.mmd. Nothing else lives in the DB.

CREATE TABLE DOCUMENT (
    doc_id   TEXT PRIMARY KEY,
    code     TEXT NOT NULL,
    name_zh  TEXT NOT NULL
);

CREATE TABLE VISA_POLICY (
    policy_id                TEXT PRIMARY KEY,
    country                  TEXT    NOT NULL,
    policy_type              TEXT    NOT NULL,
    title                    TEXT    NOT NULL,
    effective_from           DATE,
    effective_to             DATE,
    cas_required             INTEGER CHECK (cas_required IN (0,1)),
    i20_required             INTEGER CHECK (i20_required IN (0,1)),
    funds_tuition_unpaid     NUMERIC,
    funds_tuition_currency   TEXT    CHECK (funds_tuition_currency IS NULL OR funds_tuition_currency GLOB '[A-Z][A-Z][A-Z]'),
    funds_living_monthly     NUMERIC,
    funds_living_currency    TEXT    CHECK (funds_living_currency  IS NULL OR funds_living_currency  GLOB '[A-Z][A-Z][A-Z]'),
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

CREATE TABLE FX_POLICY (
    policy_id                TEXT PRIMARY KEY,
    country                  TEXT    NOT NULL,
    policy_type              TEXT    NOT NULL,
    title                    TEXT    NOT NULL,
    effective_from           DATE,
    effective_to             DATE,
    annual_quota_amount      NUMERIC,
    annual_quota_currency    TEXT    CHECK (annual_quota_currency IS NULL OR annual_quota_currency GLOB '[A-Z][A-Z][A-Z]'),
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

CREATE TABLE POLICY_DOCUMENT (
    policy_id      TEXT NOT NULL,
    policy_domain  TEXT NOT NULL CHECK (policy_domain IN ('visa_policy','fx_policy')),
    doc_id         TEXT NOT NULL,
    required       INTEGER NOT NULL CHECK (required IN (0,1)),
    "condition"    TEXT,
    PRIMARY KEY (policy_id, policy_domain, doc_id),
    FOREIGN KEY (doc_id) REFERENCES DOCUMENT(doc_id) ON DELETE RESTRICT
);

CREATE TABLE ACCOUNT_THRESHOLD (
    threshold_id          TEXT PRIMARY KEY,
    bank_code             TEXT    NOT NULL,
    product_code          TEXT    NOT NULL,
    country               TEXT    NOT NULL,
    min_age               INTEGER,
    min_deposit           NUMERIC,
    min_deposit_currency  TEXT    CHECK (min_deposit_currency IS NULL OR min_deposit_currency GLOB '[A-Z][A-Z][A-Z]'),
    residency_note        TEXT,
    CHECK (min_deposit IS NULL OR min_deposit_currency IS NOT NULL)
);

CREATE TABLE ACCOUNT_DOC (
    threshold_id   TEXT NOT NULL,
    doc_id         TEXT NOT NULL,
    required       INTEGER NOT NULL CHECK (required IN (0,1)),
    PRIMARY KEY (threshold_id, doc_id),
    FOREIGN KEY (threshold_id) REFERENCES ACCOUNT_THRESHOLD(threshold_id) ON DELETE RESTRICT,
    FOREIGN KEY (doc_id)        REFERENCES DOCUMENT(doc_id)            ON DELETE RESTRICT
);

CREATE TABLE CREDIT_CARD (
    card_id                  TEXT PRIMARY KEY,
    issuer                   TEXT    NOT NULL,
    product_name             TEXT    NOT NULL,
    annual_fee               NUMERIC,
    fee_waiver_condition     TEXT,
    target_audience          TEXT,
    overseas_cashback_rate   NUMERIC CHECK (overseas_cashback_rate IS NULL OR (overseas_cashback_rate BETWEEN 0 AND 1)),
    fx_markup_rate           NUMERIC CHECK (fx_markup_rate         IS NULL OR (fx_markup_rate         BETWEEN 0 AND 1)),
    eligibility              TEXT,
    perks                    TEXT
);

CREATE TABLE SCHOOL (
    school_id   TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    country     TEXT,
    city        TEXT
);

CREATE TABLE PROGRAM (
    program_id   TEXT PRIMARY KEY,
    school_id    TEXT NOT NULL,
    name         TEXT NOT NULL,
    level        TEXT,
    FOREIGN KEY (school_id) REFERENCES SCHOOL(school_id) ON DELETE RESTRICT
);

CREATE TABLE TUITION (
    tuition_id     TEXT PRIMARY KEY,
    program_id     TEXT NOT NULL,
    academic_year  TEXT,
    amount         NUMERIC,
    currency       TEXT    CHECK (currency IS NULL OR currency GLOB '[A-Z][A-Z][A-Z]'),
    FOREIGN KEY (program_id) REFERENCES PROGRAM(program_id) ON DELETE RESTRICT
);
