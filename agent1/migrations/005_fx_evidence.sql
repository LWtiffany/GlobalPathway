-- 005_fx_evidence.sql
-- Verbatim support for FX_POLICY values and POLICY_DOCUMENT links of the
-- fx_policy domain. POLICY_EVIDENCE references VISA_POLICY, so FX keeps its own
-- table with the same shape: target is field:<column> or document:<doc_id>.

CREATE TABLE FX_EVIDENCE (
    evidence_id  TEXT PRIMARY KEY,
    policy_id    TEXT NOT NULL REFERENCES FX_POLICY(policy_id) ON DELETE CASCADE,
    target       TEXT NOT NULL CHECK (target LIKE 'field:%' OR target LIKE 'document:%'),
    kind         TEXT NOT NULL CHECK (kind IN ('value','condition')),
    source_id    TEXT NOT NULL,
    snapshot_id  TEXT NOT NULL,
    quote        TEXT NOT NULL,
    recorded_at  TEXT NOT NULL
);
CREATE INDEX idx_fx_evidence_target ON FX_EVIDENCE(policy_id, target);
