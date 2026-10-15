-- 007_tuition_catalog.sql
-- Catalog tuition sources (programmes discovered from official fee pages):
--   * TUITION.fee_status gains US residency ('in_state','out_of_state'); SQLite
--     cannot alter a CHECK, so TUITION and TUITION_EVIDENCE are rebuilt with
--     their rows copied unchanged.
--   * PROGRAM.scope: 'programme' (one degree programme), 'division' (a school /
--     college / faculty rate), 'school_wide' (one rate for a whole level).

CREATE TABLE TUITION_EVIDENCE_007 AS SELECT * FROM TUITION_EVIDENCE;
DROP TABLE TUITION_EVIDENCE;

CREATE TABLE TUITION_007 (
    tuition_id     TEXT PRIMARY KEY,
    program_id     TEXT NOT NULL,
    academic_year  TEXT,
    amount         NUMERIC,
    currency       TEXT CHECK (currency IS NULL OR currency GLOB '[A-Z][A-Z][A-Z]'),
    fee_status     TEXT CHECK (fee_status IS NULL OR fee_status IN
                       ('home','international','local','non_local','in_state','out_of_state','all')),
    fee_basis      TEXT CHECK (fee_basis IS NULL OR fee_basis IN ('per_year','total','per_credit','per_term')),
    study_mode     TEXT CHECK (study_mode IS NULL OR study_mode IN ('full_time','part_time','any')),
    remark         TEXT,
    FOREIGN KEY (program_id) REFERENCES PROGRAM(program_id) ON DELETE RESTRICT
);
INSERT INTO TUITION_007 (tuition_id, program_id, academic_year, amount, currency, fee_status,
                         fee_basis, study_mode, remark)
    SELECT tuition_id, program_id, academic_year, amount, currency, fee_status, fee_basis,
           study_mode, remark FROM TUITION;
DROP TABLE TUITION;
ALTER TABLE TUITION_007 RENAME TO TUITION;

CREATE TABLE TUITION_EVIDENCE (
    evidence_id  TEXT PRIMARY KEY,
    tuition_id   TEXT NOT NULL REFERENCES TUITION(tuition_id) ON DELETE CASCADE,
    kind         TEXT NOT NULL CHECK (kind IN ('amount','context')),
    source_id    TEXT NOT NULL,
    snapshot_id  TEXT NOT NULL,
    quote        TEXT NOT NULL,
    recorded_at  TEXT NOT NULL
);
INSERT INTO TUITION_EVIDENCE SELECT * FROM TUITION_EVIDENCE_007;
DROP TABLE TUITION_EVIDENCE_007;
CREATE INDEX idx_tuition_evidence ON TUITION_EVIDENCE(tuition_id);

ALTER TABLE PROGRAM ADD COLUMN scope TEXT
    CHECK (scope IS NULL OR scope IN ('programme','division','school_wide'));
