-- 002_review_queue.sql
-- The single human-review inbox. Items are produced by the scan pipeline when a
-- new scope fails the gate, or when extracted values differ from the row already
-- in a domain table. `review approve` applies proposed_value to the domain table.

CREATE TABLE REVIEW_QUEUE (
    review_id       TEXT PRIMARY KEY,
    domain_table    TEXT NOT NULL CHECK (domain_table IN ('VISA_POLICY','FX_POLICY','ACCOUNT_THRESHOLD','CREDIT_CARD','SCHOOL','PROGRAM','TUITION')),
    record_key      TEXT NOT NULL,
    action          TEXT NOT NULL CHECK (action IN ('CREATE','UPDATE')),
    field_path      TEXT,
    proposed_value  TEXT,
    evidence_json   TEXT,
    reason          TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'OPEN' CHECK (status IN ('OPEN','APPROVED','REJECTED')),
    decided_by      TEXT,
    decided_at      TEXT,
    decision_note   TEXT,
    created_at      TEXT NOT NULL
);
