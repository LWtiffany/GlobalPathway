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

CREATE TABLE ACCOUNT_PRODUCT (
    account_id                 TEXT PRIMARY KEY,
    product_code               TEXT NOT NULL UNIQUE,
    product_name               TEXT NOT NULL,
    market                     TEXT NOT NULL CHECK (market IN ('UK','US','HK')),
    account_type               TEXT NOT NULL CHECK (account_type IN ('current','checking','savings','integrated')),
    customer_segment           TEXT NOT NULL DEFAULT 'general'
        CHECK (customer_segment IN ('general','student','international_student','premier')),
    primary_currency           TEXT NOT NULL CHECK (primary_currency GLOB '[A-Z][A-Z][A-Z]'),
    product_summary            TEXT,
    dedicated_student_product  INTEGER NOT NULL DEFAULT 0 CHECK (dedicated_student_product IN (0,1)),
    product_status             TEXT NOT NULL DEFAULT 'ACTIVE'
        CHECK (product_status IN ('ACTIVE','SUSPENDED','DISCONTINUED')),
    official_url               TEXT,
    data_status                TEXT NOT NULL DEFAULT 'verified'
        CHECK (data_status IN ('verified','partial','estimated')),
    effective_from             TEXT,
    effective_to               TEXT,
    last_verified_at           TEXT NOT NULL,
    CHECK (effective_to IS NULL OR effective_from IS NULL OR effective_to >= effective_from)
);
CREATE INDEX idx_account_product_market ON ACCOUNT_PRODUCT(market, product_status);

CREATE TABLE ACCOUNT_OPENING_ROUTE (
    route_id                  TEXT PRIMARY KEY,
    account_id                TEXT NOT NULL REFERENCES ACCOUNT_PRODUCT(account_id) ON DELETE CASCADE,
    route_name                TEXT NOT NULL,
    application_channel       TEXT NOT NULL CHECK (application_channel IN ('mobile_app','online','branch','international_banking')),
    applicant_location        TEXT,
    identity_document_type    TEXT,
    can_apply_before_arrival  INTEGER NOT NULL DEFAULT 0 CHECK (can_apply_before_arrival IN (0,1)),
    branch_visit_required     INTEGER NOT NULL DEFAULT 0 CHECK (branch_visit_required IN (0,1)),
    activation_location       TEXT,
    processing_time_note      TEXT,
    additional_notes          TEXT,
    source_url                TEXT,
    last_verified_at          TEXT NOT NULL
);
CREATE INDEX idx_account_route_product ON ACCOUNT_OPENING_ROUTE(account_id);

CREATE TABLE ACCOUNT_ELIGIBILITY_RULE (
    eligibility_rule_id  TEXT PRIMARY KEY,
    account_id           TEXT NOT NULL REFERENCES ACCOUNT_PRODUCT(account_id) ON DELETE CASCADE,
    route_id             TEXT REFERENCES ACCOUNT_OPENING_ROUTE(route_id) ON DELETE CASCADE,
    rule_type            TEXT NOT NULL CHECK (rule_type IN ('minimum_age','maximum_age','student_status','course_status','residency','residency_duration','nationality','visa_status','applicant_location','minimum_deposit','relationship_balance','income','direct_deposit','existing_customer','other')),
    operator             TEXT NOT NULL CHECK (operator IN ('eq','neq','gte','lte','in','not_in','required','one_of')),
    value_text           TEXT,
    value_numeric        NUMERIC,
    currency             TEXT CHECK (currency IS NULL OR currency GLOB '[A-Z][A-Z][A-Z]'),
    required             INTEGER NOT NULL DEFAULT 1 CHECK (required IN (0,1)),
    description          TEXT NOT NULL,
    source_url           TEXT,
    last_verified_at     TEXT NOT NULL,
    CHECK (value_text IS NOT NULL OR value_numeric IS NOT NULL OR operator = 'required'),
    CHECK (currency IS NULL OR value_numeric IS NOT NULL)
);
CREATE INDEX idx_account_eligibility_product ON ACCOUNT_ELIGIBILITY_RULE(account_id);
CREATE INDEX idx_account_eligibility_route ON ACCOUNT_ELIGIBILITY_RULE(route_id);

CREATE TABLE ACCOUNT_REQUIRED_DOCUMENT (
    requirement_id       TEXT PRIMARY KEY,
    account_id           TEXT NOT NULL REFERENCES ACCOUNT_PRODUCT(account_id) ON DELETE CASCADE,
    route_id             TEXT REFERENCES ACCOUNT_OPENING_ROUTE(route_id) ON DELETE CASCADE,
    document_type        TEXT NOT NULL,
    document_name        TEXT NOT NULL,
    required             INTEGER NOT NULL DEFAULT 1 CHECK (required IN (0,1)),
    alternative_group    TEXT,
    condition_note       TEXT,
    validity_note        TEXT,
    source_url           TEXT,
    last_verified_at     TEXT NOT NULL
);
CREATE INDEX idx_account_document_product ON ACCOUNT_REQUIRED_DOCUMENT(account_id);
CREATE INDEX idx_account_document_route ON ACCOUNT_REQUIRED_DOCUMENT(route_id);

CREATE TABLE ACCOUNT_FEE (
    fee_id             TEXT PRIMARY KEY,
    account_id         TEXT NOT NULL REFERENCES ACCOUNT_PRODUCT(account_id) ON DELETE CASCADE,
    fee_type           TEXT NOT NULL CHECK (fee_type IN ('monthly_fee','below_balance_fee','minimum_opening_deposit','minimum_relationship_balance','international_transfer_fee','incoming_transfer_fee','foreign_transaction_fee','overseas_atm_fee','overdraft_interest','other')),
    amount             NUMERIC CHECK (amount IS NULL OR amount >= 0),
    rate               NUMERIC CHECK (rate IS NULL OR rate >= 0),
    currency           TEXT CHECK (currency IS NULL OR currency GLOB '[A-Z][A-Z][A-Z]'),
    minimum_amount     NUMERIC,
    maximum_amount     NUMERIC,
    waiver_available   INTEGER NOT NULL DEFAULT 0 CHECK (waiver_available IN (0,1)),
    waiver_condition   TEXT,
    description        TEXT NOT NULL,
    source_url         TEXT,
    last_verified_at   TEXT NOT NULL,
    CHECK (amount IS NOT NULL OR rate IS NOT NULL),
    CHECK (amount IS NULL OR currency IS NOT NULL)
);
CREATE INDEX idx_account_fee_product ON ACCOUNT_FEE(account_id);

CREATE TABLE ACCOUNT_FEATURE (
    feature_id         TEXT PRIMARY KEY,
    account_id         TEXT NOT NULL REFERENCES ACCOUNT_PRODUCT(account_id) ON DELETE CASCADE,
    feature_type       TEXT NOT NULL CHECK (feature_type IN ('debit_card','mobile_banking','mobile_wallet','multi_currency','international_transfer','global_account_transfer','atm_access','overdraft','interest','deposit_protection','other')),
    available          INTEGER NOT NULL CHECK (available IN (0,1)),
    value_text         TEXT,
    description        TEXT,
    source_url         TEXT,
    last_verified_at   TEXT NOT NULL
);
CREATE INDEX idx_account_feature_product ON ACCOUNT_FEATURE(account_id);

CREATE TABLE CREDIT_CARD (
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
    effective_from    TEXT,
    effective_to      TEXT,
    last_verified_at  TEXT,
    UNIQUE (issuing_bank, market, product_name),
    CHECK (effective_to IS NULL OR effective_from IS NULL OR effective_to >= effective_from)
);

CREATE TABLE CARD_REWARD_RULE (
    reward_rule_id           TEXT PRIMARY KEY,
    card_id                  TEXT NOT NULL REFERENCES CREDIT_CARD(card_id) ON DELETE CASCADE,
    spend_category           TEXT NOT NULL,
    reward_type              TEXT NOT NULL CHECK (reward_type IN ('cashback','points','miles')),
    reward_rate              NUMERIC CHECK (reward_rate IS NULL OR reward_rate BETWEEN 0 AND 1),
    points_per_currency_unit NUMERIC CHECK (points_per_currency_unit IS NULL OR points_per_currency_unit >= 0),
    cash_equivalent_rate     NUMERIC CHECK (cash_equivalent_rate IS NULL OR cash_equivalent_rate BETWEEN 0 AND 1),
    spend_cap                NUMERIC CHECK (spend_cap IS NULL OR spend_cap >= 0),
    cap_currency             TEXT CHECK (cap_currency IS NULL OR cap_currency GLOB '[A-Z][A-Z][A-Z]'),
    cap_period               TEXT CHECK (cap_period IS NULL OR cap_period IN ('transaction','month','quarter','year')),
    qualification_type       TEXT NOT NULL DEFAULT 'automatic'
        CHECK (qualification_type IN ('automatic','selected_category','payment_method','designated_merchant')),
    qualification_value      TEXT,
    description              TEXT,
    source_url               TEXT,
    last_verified_at         TIMESTAMPTZ,
    CHECK (reward_rate IS NOT NULL OR points_per_currency_unit IS NOT NULL),
    CHECK (spend_cap IS NULL OR cap_currency IS NOT NULL)
);
CREATE INDEX idx_card_reward_rule_card ON CARD_REWARD_RULE(card_id);

CREATE TABLE CARD_FEE (
    fee_id            TEXT PRIMARY KEY,
    card_id           TEXT NOT NULL REFERENCES CREDIT_CARD(card_id) ON DELETE CASCADE,
    fee_type          TEXT NOT NULL CHECK (fee_type IN ('annual_fee','foreign_transaction_fee','cash_advance_fee','late_payment_fee','other')),
    amount            NUMERIC CHECK (amount IS NULL OR amount >= 0),
    currency          TEXT CHECK (currency IS NULL OR currency GLOB '[A-Z][A-Z][A-Z]'),
    rate              NUMERIC CHECK (rate IS NULL OR rate BETWEEN 0 AND 1),
    waiver_condition  TEXT,
    notes             TEXT,
    scope_text        TEXT,
    source_url        TEXT,
    last_verified_at  TIMESTAMPTZ,
    CHECK (amount IS NOT NULL OR rate IS NOT NULL),
    CHECK (amount IS NULL OR currency IS NOT NULL)
);
CREATE INDEX idx_card_fee_card ON CARD_FEE(card_id);

CREATE TABLE CARD_BORROWING_TERM (
    borrowing_term_id       TEXT PRIMARY KEY,
    card_id                 TEXT NOT NULL REFERENCES CREDIT_CARD(card_id) ON DELETE CASCADE,
    term_type               TEXT NOT NULL CHECK (term_type IN (
        'purchase_apr','cash_advance_apr','interest_free_period',
        'cash_instalment_offer','cash_instalment_example','spending_instalment'
    )),
    apr                     NUMERIC CHECK (apr IS NULL OR apr BETWEEN 0 AND 1),
    monthly_fee_rate        NUMERIC CHECK (monthly_fee_rate IS NULL OR monthly_fee_rate BETWEEN 0 AND 1),
    tenor_months            INTEGER CHECK (tenor_months IS NULL OR tenor_months > 0),
    max_interest_free_days  INTEGER CHECK (max_interest_free_days IS NULL OR max_interest_free_days >= 0),
    example_principal       NUMERIC CHECK (example_principal IS NULL OR example_principal >= 0),
    periodic_repayment      NUMERIC CHECK (periodic_repayment IS NULL OR periodic_repayment >= 0),
    total_repayment         NUMERIC CHECK (total_repayment IS NULL OR total_repayment >= 0),
    currency                TEXT CHECK (currency IS NULL OR currency GLOB '[A-Z][A-Z][A-Z]'),
    available               INTEGER NOT NULL DEFAULT 1 CHECK (available IN (0,1)),
    personalized_pricing    INTEGER NOT NULL DEFAULT 0 CHECK (personalized_pricing IN (0,1)),
    description             TEXT NOT NULL,
    source_url              TEXT NOT NULL,
    source_effective_date   TEXT,
    last_verified_at        TEXT NOT NULL,
    UNIQUE (card_id, term_type, tenor_months),
    CHECK (example_principal IS NULL OR currency IS NOT NULL),
    CHECK (periodic_repayment IS NULL OR currency IS NOT NULL),
    CHECK (total_repayment IS NULL OR currency IS NOT NULL)
);
CREATE INDEX idx_card_borrowing_term_card ON CARD_BORROWING_TERM(card_id);

CREATE TABLE CARD_BENEFIT (
    benefit_id    TEXT PRIMARY KEY,
    card_id       TEXT NOT NULL REFERENCES CREDIT_CARD(card_id) ON DELETE CASCADE,
    benefit_type  TEXT NOT NULL CHECK (benefit_type IN ('airport_lounge','travel_insurance','concierge','merchant_offer','digital_wallet','dual_currency','other')),
    benefit_name  TEXT NOT NULL,
    quantity      NUMERIC CHECK (quantity IS NULL OR quantity >= 0),
    period        TEXT CHECK (period IS NULL OR period IN ('transaction','month','quarter','year')),
    description   TEXT,
    source_url    TEXT,
    last_verified_at TIMESTAMPTZ
);
CREATE INDEX idx_card_benefit_card ON CARD_BENEFIT(card_id);

CREATE TABLE CARD_ELIGIBILITY_RULE (
    eligibility_rule_id  TEXT PRIMARY KEY,
    card_id              TEXT NOT NULL REFERENCES CREDIT_CARD(card_id) ON DELETE CASCADE,
    rule_type            TEXT NOT NULL CHECK (rule_type IN ('minimum_age','minimum_income','minimum_assets','asset_proof','student_status','institution_status','banking_relationship','relationship_balance','credit_assessment','residency','other')),
    operator             TEXT NOT NULL CHECK (operator IN ('eq','gte','lte','in','required','available')),
    value_text           TEXT,
    value_numeric        NUMERIC,
    currency             TEXT CHECK (currency IS NULL OR currency GLOB '[A-Z][A-Z][A-Z]'),
    required             INTEGER NOT NULL DEFAULT 1 CHECK (required IN (0,1)),
    path_id              TEXT NOT NULL DEFAULT 'hard',
    outcome_if_met       TEXT NOT NULL DEFAULT 'meets_public_criteria'
        CHECK (outcome_if_met IN ('meets_public_criteria','needs_bank_assessment')),
    application_channel  TEXT NOT NULL DEFAULT 'all'
        CHECK (application_channel IN ('all','digital','phone','document_submission')),
    description          TEXT,
    source_url           TEXT,
    last_verified_at     TEXT,
    CHECK (value_text IS NOT NULL OR value_numeric IS NOT NULL),
    CHECK (currency IS NULL OR value_numeric IS NOT NULL)
);
CREATE INDEX idx_card_eligibility_card ON CARD_ELIGIBILITY_RULE(card_id);
CREATE INDEX idx_card_eligibility_path ON CARD_ELIGIBILITY_RULE(card_id, path_id);

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
