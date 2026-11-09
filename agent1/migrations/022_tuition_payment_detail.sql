-- 020_tuition_payment_detail.sql
-- Billing cadence + accepted payment methods beyond a single tuition amount.
-- Rows are scoped school_wide / level / programme so programme overrides apply.

CREATE TABLE IF NOT EXISTS TUITION_PAYMENT_DETAIL (
    detail_id            TEXT PRIMARY KEY,
    school_id            TEXT NOT NULL REFERENCES school(school_id),
    program_id           TEXT REFERENCES program(program_id) ON DELETE CASCADE,
    scope                TEXT NOT NULL CHECK (scope IN ('school_wide', 'level', 'programme')),
    level                TEXT,
    academic_year        TEXT,
    billing_cycle        TEXT NOT NULL
        CHECK (billing_cycle IN (
            'per_year_advance',   -- full year due up front / Week 1
            'per_term',           -- semester / term bills
            'per_quarter',        -- academic quarter
            'monthly',            -- monthly within term/quarter
            'installment',        -- fixed N instalments (e.g. 2 halves)
            'other'
        )),
    installment_count    INTEGER,
    installment_period   TEXT CHECK (
        installment_period IS NULL OR installment_period IN (
            'month', 'term', 'quarter', 'year', 'other')),
    payment_methods      JSONB NOT NULL DEFAULT '[]'::jsonb,
    accepts_credit_card  INTEGER CHECK (accepts_credit_card IS NULL OR accepts_credit_card IN (0, 1)),
    portal_name          TEXT,
    portal_url           TEXT,
    payee                TEXT,          -- college / university / department
    due_rule             TEXT,          -- human-readable due rule from source
    summary              TEXT NOT NULL, -- short structured summary
    quote                TEXT NOT NULL, -- verbatim evidence excerpt
    source_url           TEXT NOT NULL,
    recorded_at          TEXT NOT NULL,
    CHECK (
        (scope = 'school_wide' AND program_id IS NULL AND level IS NULL)
        OR (scope = 'level' AND program_id IS NULL AND level IS NOT NULL)
        OR (scope = 'programme' AND program_id IS NOT NULL)
    )
);

CREATE INDEX IF NOT EXISTS idx_tuition_payment_school
    ON TUITION_PAYMENT_DETAIL(school_id);
CREATE INDEX IF NOT EXISTS idx_tuition_payment_program
    ON TUITION_PAYMENT_DETAIL(program_id);

-- Allow quarterly fee_basis on TUITION amount rows when pages publish that way.
ALTER TABLE TUITION DROP CONSTRAINT IF EXISTS tuition_fee_basis_check;
ALTER TABLE TUITION ADD CONSTRAINT tuition_fee_basis_check
    CHECK (fee_basis IS NULL OR fee_basis IN (
        'per_year', 'total', 'per_credit', 'per_term', 'per_quarter', 'monthly'));
