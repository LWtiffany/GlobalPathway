-- Remove the coarse card-level data label and store borrowing costs with
-- field-level provenance instead. PostgreSQL DDL is transactional.
ALTER TABLE CREDIT_CARD DROP COLUMN IF EXISTS data_status;

CREATE TABLE IF NOT EXISTS CARD_BORROWING_TERM (
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
    currency                TEXT CHECK (currency IS NULL OR currency ~ '^[A-Z]{3}$'),
    available               BOOLEAN NOT NULL DEFAULT TRUE,
    personalized_pricing    BOOLEAN NOT NULL DEFAULT FALSE,
    description             TEXT NOT NULL,
    source_url              TEXT NOT NULL,
    source_effective_date   DATE,
    last_verified_at        TIMESTAMPTZ NOT NULL,
    UNIQUE (card_id, term_type, tenor_months),
    CHECK (example_principal IS NULL OR currency IS NOT NULL),
    CHECK (periodic_repayment IS NULL OR currency IS NOT NULL),
    CHECK (total_repayment IS NULL OR currency IS NOT NULL)
);
CREATE INDEX IF NOT EXISTS idx_card_borrowing_term_card ON CARD_BORROWING_TERM(card_id);

ALTER TABLE CARD_BORROWING_TERM
    DROP CONSTRAINT IF EXISTS card_borrowing_term_term_type_check;
ALTER TABLE CARD_BORROWING_TERM
    ADD CONSTRAINT card_borrowing_term_term_type_check CHECK (term_type IN (
        'purchase_apr','cash_advance_apr','interest_free_period',
        'cash_instalment_offer','cash_instalment_example','spending_instalment'
    ));
