-- 001_admin.sql
-- Admin database (admin.sqlite3, next to agent1.sqlite3): administrator accounts
-- and the human-review inbox. Agent1 sessions attach it as schema `admin`.

CREATE TABLE ADMIN_USER (
    username        TEXT PRIMARY KEY CHECK (length(username) BETWEEN 1 AND 40),
    password_hash   TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    last_login_at   TEXT
);

-- One row per undecided item. Approving applies proposed_value to the Agent1
-- domain table and deletes the row; rejecting only deletes it.
CREATE TABLE REVIEW_QUEUE (
    review_id       TEXT PRIMARY KEY,
    domain_table    TEXT NOT NULL CHECK (domain_table IN ('VISA_POLICY','FX_POLICY','ACCOUNT_THRESHOLD','CREDIT_CARD','SCHOOL','PROGRAM','TUITION')),
    record_key      TEXT NOT NULL,
    action          TEXT NOT NULL CHECK (action IN ('CREATE','UPDATE')),
    field_path      TEXT,
    proposed_value  TEXT,
    evidence_json   TEXT,
    reason          TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    UNIQUE (domain_table, record_key, action)
);
