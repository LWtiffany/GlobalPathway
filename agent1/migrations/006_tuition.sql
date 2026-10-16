-- 006_tuition.sql
-- Tuition capture (schema_type tuition). A TUITION row is one price point of a
-- programme: (academic_year, fee_status, fee_basis, study_mode) qualify amount.
-- Values not stated by the page stay NULL; 'all' / 'any' mean the page states
-- the fee applies regardless of status / study mode.

ALTER TABLE TUITION ADD COLUMN fee_status TEXT
    CHECK (fee_status IS NULL OR fee_status IN ('home','international','local','non_local','all'));
ALTER TABLE TUITION ADD COLUMN fee_basis TEXT
    CHECK (fee_basis IS NULL OR fee_basis IN ('per_year','total','per_credit','per_term'));
ALTER TABLE TUITION ADD COLUMN study_mode TEXT
    CHECK (study_mode IS NULL OR study_mode IN ('full_time','part_time','any'));
ALTER TABLE TUITION ADD COLUMN remark TEXT;

-- Verbatim support: kind 'amount' is the quote holding amount + currency;
-- 'context' quotes name the programme / fee status / academic year near it.
CREATE TABLE TUITION_EVIDENCE (
    evidence_id  TEXT PRIMARY KEY,
    tuition_id   TEXT NOT NULL REFERENCES TUITION(tuition_id) ON DELETE CASCADE,
    kind         TEXT NOT NULL CHECK (kind IN ('amount','context')),
    source_id    TEXT NOT NULL,
    snapshot_id  TEXT NOT NULL,
    quote        TEXT NOT NULL,
    recorded_at  TEXT NOT NULL
);
CREATE INDEX idx_tuition_evidence ON TUITION_EVIDENCE(tuition_id);
