-- Eligibility paths are OR alternatives; rules inside one path are AND conditions.
-- The special `hard` path contains conditions that apply to every route.
ALTER TABLE CARD_ELIGIBILITY_RULE
    ADD COLUMN IF NOT EXISTS path_id TEXT NOT NULL DEFAULT 'hard',
    ADD COLUMN IF NOT EXISTS outcome_if_met TEXT NOT NULL DEFAULT 'meets_public_criteria',
    ADD COLUMN IF NOT EXISTS application_channel TEXT NOT NULL DEFAULT 'all',
    ADD COLUMN IF NOT EXISTS source_url TEXT,
    ADD COLUMN IF NOT EXISTS last_verified_at TIMESTAMPTZ;

ALTER TABLE CARD_ELIGIBILITY_RULE
    DROP CONSTRAINT IF EXISTS card_eligibility_rule_rule_type_check,
    DROP CONSTRAINT IF EXISTS card_eligibility_rule_operator_check,
    DROP CONSTRAINT IF EXISTS card_eligibility_rule_outcome_if_met_check,
    DROP CONSTRAINT IF EXISTS card_eligibility_rule_application_channel_check;

ALTER TABLE CARD_ELIGIBILITY_RULE
    ADD CONSTRAINT card_eligibility_rule_rule_type_check CHECK (rule_type IN (
        'minimum_age','minimum_income','minimum_assets','asset_proof','student_status',
        'institution_status','banking_relationship','relationship_balance',
        'credit_assessment','residency','other'
    )),
    ADD CONSTRAINT card_eligibility_rule_operator_check CHECK (operator IN (
        'eq','gte','lte','in','required','available'
    )),
    ADD CONSTRAINT card_eligibility_rule_outcome_if_met_check CHECK (outcome_if_met IN (
        'meets_public_criteria','needs_bank_assessment'
    )),
    ADD CONSTRAINT card_eligibility_rule_application_channel_check CHECK (application_channel IN (
        'all','digital','phone','document_submission'
    ));

CREATE INDEX IF NOT EXISTS idx_card_eligibility_path
    ON CARD_ELIGIBILITY_RULE(card_id, path_id);
