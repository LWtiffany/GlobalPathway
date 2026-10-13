-- 003_gate_issue.sql
-- Gate problems produced by a scan run. Every problem the gate reports (error or
-- warning) is persisted here so the "why" behind a blocked/queued value is
-- queryable in the database instead of only living in the REVIEW_QUEUE reason
-- string. OPEN rows are deduplicated by issue_key; an OPEN row that no longer
-- appears in the latest report for its source is auto-resolved.

CREATE TABLE GATE_ISSUE (
    issue_id         TEXT PRIMARY KEY,
    source_id        TEXT NOT NULL,
    snapshot_id      TEXT,
    scope            TEXT,
    check_name       TEXT NOT NULL,
    severity         TEXT NOT NULL CHECK (severity IN ('error','warning')),
    field            TEXT,
    value            TEXT,
    quote            TEXT,
    reason           TEXT NOT NULL,
    suggested_action TEXT,
    issue_key        TEXT NOT NULL,
    status           TEXT NOT NULL DEFAULT 'OPEN' CHECK (status IN ('OPEN','RESOLVED','IGNORED')),
    first_seen_at    TEXT NOT NULL,
    last_seen_at     TEXT NOT NULL,
    resolved_at      TEXT
);

CREATE INDEX idx_gate_issue_source_status ON GATE_ISSUE(source_id, status);
CREATE INDEX idx_gate_issue_key ON GATE_ISSUE(source_id, issue_key);
