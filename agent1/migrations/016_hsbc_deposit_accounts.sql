-- HSBC deposit-account catalog for Agent1 PostgreSQL.
-- This supplements legacy ACCOUNT_THRESHOLD / ACCOUNT_DOC without changing them.
-- It is deliberately independent of Agent2 routing and recommendation code.

CREATE TABLE IF NOT EXISTS ACCOUNT_PRODUCT (
    account_id TEXT PRIMARY KEY,
    bank_code TEXT NOT NULL,
    market TEXT NOT NULL CHECK (market IN ('UK', 'US', 'CN-HK')),
    product_name TEXT NOT NULL,
    account_type TEXT NOT NULL CHECK (account_type IN ('current', 'checking', 'savings', 'integrated')),
    customer_segment TEXT NOT NULL,
    summary TEXT NOT NULL,
    product_status TEXT NOT NULL DEFAULT 'ACTIVE'
        CHECK (product_status IN ('ACTIVE', 'SUSPENDED', 'DISCONTINUED')),
    official_url TEXT NOT NULL,
    verified_at DATE NOT NULL
);

CREATE TABLE IF NOT EXISTS ACCOUNT_OPENING_ROUTE (
    route_id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL REFERENCES ACCOUNT_PRODUCT(account_id),
    route_name TEXT NOT NULL,
    channel TEXT NOT NULL CHECK (channel IN ('app', 'online', 'branch', 'callback')),
    applicant_location TEXT,
    identity_document TEXT,
    before_arrival BOOLEAN,
    activation_note TEXT,
    route_note TEXT,
    source_url TEXT NOT NULL,
    verified_at DATE NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_account_route_product ON ACCOUNT_OPENING_ROUTE(account_id);

CREATE TABLE IF NOT EXISTS ACCOUNT_RULE (
    rule_id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL REFERENCES ACCOUNT_PRODUCT(account_id),
    route_id TEXT REFERENCES ACCOUNT_OPENING_ROUTE(route_id),
    rule_purpose TEXT NOT NULL CHECK (rule_purpose IN
        ('application', 'route', 'fee_waiver', 'disclosure')),
    -- Different group codes are alternatives; rules within a group are cumulative.
    -- The agent must not infer a decision from these rows without a rule evaluator.
    group_code TEXT,
    field_code TEXT NOT NULL,
    operator TEXT NOT NULL CHECK (operator IN ('eq', 'gte', 'lte', 'in', 'present', 'note')),
    value_text TEXT,
    value_numeric NUMERIC,
    currency TEXT,
    condition_note TEXT,
    description TEXT NOT NULL,
    source_url TEXT NOT NULL,
    verified_at DATE NOT NULL,
    CHECK (value_text IS NOT NULL OR value_numeric IS NOT NULL OR operator IN ('present', 'note'))
);
CREATE INDEX IF NOT EXISTS idx_account_rule_product ON ACCOUNT_RULE(account_id, rule_purpose);
CREATE INDEX IF NOT EXISTS idx_account_rule_route ON ACCOUNT_RULE(route_id);

CREATE TABLE IF NOT EXISTS ACCOUNT_REQUIRED_DOCUMENT (
    requirement_id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL REFERENCES ACCOUNT_PRODUCT(account_id),
    route_id TEXT REFERENCES ACCOUNT_OPENING_ROUTE(route_id),
    document_type TEXT NOT NULL,
    description TEXT NOT NULL,
    requirement_level TEXT NOT NULL CHECK (requirement_level IN ('required', 'may_request')),
    alternative_group TEXT,
    condition_note TEXT,
    source_url TEXT NOT NULL,
    verified_at DATE NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_account_document_product ON ACCOUNT_REQUIRED_DOCUMENT(account_id);

CREATE TABLE IF NOT EXISTS ACCOUNT_FEE (
    fee_id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL REFERENCES ACCOUNT_PRODUCT(account_id),
    fee_type TEXT NOT NULL CHECK (fee_type IN ('monthly_account', 'below_balance')),
    amount NUMERIC NOT NULL CHECK (amount >= 0),
    currency TEXT NOT NULL,
    condition_note TEXT,
    waiver_note TEXT,
    effective_from DATE,
    source_url TEXT NOT NULL,
    verified_at DATE NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_account_fee_product ON ACCOUNT_FEE(account_id);
