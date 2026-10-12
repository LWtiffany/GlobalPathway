-- Remaining domains from the supplied ER diagram. Data ingestion/publication is not wired yet.
CREATE TABLE fx_policy (
    policy_id TEXT PRIMARY KEY NOT NULL,
    source_id TEXT NOT NULL REFERENCES sources(source_id) ON DELETE RESTRICT,
    country TEXT NOT NULL,
    policy_type TEXT NOT NULL,
    title TEXT NOT NULL,
    effective_from TEXT,
    effective_to TEXT,
    annual_quota_amount NUMERIC CHECK(annual_quota_amount IS NULL OR (typeof(annual_quota_amount) IN ('integer','real') AND annual_quota_amount >= 0)),
    annual_quota_currency TEXT CHECK(annual_quota_currency IS NULL OR annual_quota_currency GLOB '[A-Z][A-Z][A-Z]'),
    quota_reset TEXT,
    over_quota_allowed INTEGER CHECK(over_quota_allowed IN (0,1)),
    purpose_code TEXT,
    tuition_direct_to_school INTEGER CHECK(tuition_direct_to_school IN (0,1)),
    remitter_relation TEXT,
    can_use_family_quota INTEGER CHECK(can_use_family_quota IN (0,1)),
    remark TEXT,
    created_at TEXT NOT NULL,
    CHECK(annual_quota_amount IS NULL OR annual_quota_currency IS NOT NULL),
    CHECK(effective_from IS NULL OR effective_to IS NULL OR effective_from <= effective_to)
);

-- Separate typed link avoids a policy_id that points to one of several unrelated tables.
CREATE TABLE fx_policy_document (
    policy_id TEXT NOT NULL REFERENCES fx_policy(policy_id) ON DELETE RESTRICT,
    doc_id TEXT NOT NULL REFERENCES document(doc_id) ON DELETE RESTRICT,
    requirement_key TEXT NOT NULL,
    required INTEGER CHECK(required IN (0,1)),
    applicability_status TEXT NOT NULL CHECK(applicability_status IN ('unconditional','conditional','unknown')),
    condition_json TEXT CHECK(condition_json IS NULL OR (json_valid(condition_json) AND json_type(condition_json)='object')),
    PRIMARY KEY(policy_id, doc_id, requirement_key),
    CHECK((applicability_status='conditional' AND condition_json IS NOT NULL AND condition_json <> '{}')
       OR (applicability_status='unconditional' AND condition_json IS NULL)
       OR (applicability_status='unknown' AND condition_json IS NULL AND required IS NULL))
);

CREATE TABLE account_threshold (
    threshold_id TEXT PRIMARY KEY NOT NULL,
    source_id TEXT NOT NULL REFERENCES sources(source_id) ON DELETE RESTRICT,
    bank_code TEXT NOT NULL,
    product_code TEXT NOT NULL,
    country TEXT NOT NULL,
    min_age INTEGER CHECK(typeof(min_age)='null' OR (typeof(min_age)='integer' AND min_age >= 0)),
    min_deposit NUMERIC CHECK(min_deposit IS NULL OR (typeof(min_deposit) IN ('integer','real') AND min_deposit >= 0)),
    min_deposit_currency TEXT CHECK(min_deposit_currency IS NULL OR min_deposit_currency GLOB '[A-Z][A-Z][A-Z]'),
    residency_note TEXT,
    created_at TEXT NOT NULL,
    CHECK(min_deposit IS NULL OR min_deposit_currency IS NOT NULL)
);

CREATE TABLE account_doc (
    threshold_id TEXT NOT NULL REFERENCES account_threshold(threshold_id) ON DELETE RESTRICT,
    doc_id TEXT NOT NULL REFERENCES document(doc_id) ON DELETE RESTRICT,
    required INTEGER CHECK(required IN (0,1)),
    PRIMARY KEY(threshold_id, doc_id)
);

CREATE TABLE credit_card (
    card_id TEXT PRIMARY KEY NOT NULL,
    source_id TEXT NOT NULL REFERENCES sources(source_id) ON DELETE RESTRICT,
    issuer TEXT NOT NULL,
    product_name TEXT NOT NULL,
    annual_fee NUMERIC CHECK(annual_fee IS NULL OR (typeof(annual_fee) IN ('integer','real') AND annual_fee >= 0)),
    annual_fee_currency TEXT CHECK(annual_fee_currency IS NULL OR annual_fee_currency GLOB '[A-Z][A-Z][A-Z]'),
    fee_waiver_condition TEXT,
    target_audience TEXT,
    overseas_cashback_rate NUMERIC CHECK(overseas_cashback_rate IS NULL OR (typeof(overseas_cashback_rate) IN ('integer','real') AND overseas_cashback_rate BETWEEN 0 AND 1)),
    fx_markup_rate NUMERIC CHECK(fx_markup_rate IS NULL OR (typeof(fx_markup_rate) IN ('integer','real') AND fx_markup_rate BETWEEN 0 AND 1)),
    eligibility TEXT,
    perks_json TEXT CHECK(perks_json IS NULL OR json_valid(perks_json)),
    created_at TEXT NOT NULL,
    CHECK(annual_fee IS NULL OR annual_fee_currency IS NOT NULL)
);

CREATE TABLE school (
    school_id TEXT PRIMARY KEY NOT NULL,
    source_id TEXT NOT NULL REFERENCES sources(source_id) ON DELETE RESTRICT,
    name TEXT NOT NULL,
    country TEXT NOT NULL,
    city TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE program (
    program_id TEXT PRIMARY KEY NOT NULL,
    source_id TEXT NOT NULL REFERENCES sources(source_id) ON DELETE RESTRICT,
    school_id TEXT NOT NULL REFERENCES school(school_id) ON DELETE RESTRICT,
    name TEXT NOT NULL,
    level TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE tuition (
    tuition_id TEXT PRIMARY KEY NOT NULL,
    source_id TEXT NOT NULL REFERENCES sources(source_id) ON DELETE RESTRICT,
    program_id TEXT NOT NULL REFERENCES program(program_id) ON DELETE RESTRICT,
    academic_year TEXT NOT NULL,
    amount NUMERIC CHECK(amount IS NULL OR (typeof(amount) IN ('integer','real') AND amount >= 0)),
    currency TEXT CHECK(currency IS NULL OR currency GLOB '[A-Z][A-Z][A-Z]'),
    created_at TEXT NOT NULL,
    CHECK(amount IS NULL OR currency IS NOT NULL)
);

CREATE INDEX tuition_by_program_year ON tuition(program_id, academic_year);
