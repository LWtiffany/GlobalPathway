-- 004_publication.sql
-- Publication surface for VISA_POLICY: the working rows written by scans are never
-- read by Agent2 directly. A human publishes a policy, which seals its current
-- fields, documents and verbatim evidence into an immutable POLICY_REVISION and
-- points POLICY_PUBLICATION at it. Withdrawal flips the pointer; nothing is deleted.

-- Material applicability is tri-state: required=1 with/without condition, required=0
-- (mentioned only), required NULL (the source states a condition that could not be
-- confirmed; never to be read as an unconditional requirement).
CREATE TABLE POLICY_DOCUMENT_V2 (
    policy_id      TEXT NOT NULL,
    policy_domain  TEXT NOT NULL CHECK (policy_domain IN ('visa_policy','fx_policy')),
    doc_id         TEXT NOT NULL,
    required       INTEGER CHECK (required IS NULL OR required IN (0,1)),
    "condition"    TEXT,
    CHECK (required IS NOT NULL OR "condition" IS NULL),
    PRIMARY KEY (policy_id, policy_domain, doc_id),
    FOREIGN KEY (doc_id) REFERENCES DOCUMENT(doc_id) ON DELETE RESTRICT
);
INSERT INTO POLICY_DOCUMENT_V2 (policy_id, policy_domain, doc_id, required, "condition")
    SELECT policy_id, policy_domain, doc_id, required, "condition" FROM POLICY_DOCUMENT;
DROP TABLE POLICY_DOCUMENT;
ALTER TABLE POLICY_DOCUMENT_V2 RENAME TO POLICY_DOCUMENT;

-- Verbatim support for the values currently stored in a working row.
-- target: 'field:<VISA_POLICY column>' or 'document:<doc_id>'.
CREATE TABLE POLICY_EVIDENCE (
    evidence_id  TEXT PRIMARY KEY,
    policy_id    TEXT NOT NULL REFERENCES VISA_POLICY(policy_id) ON DELETE CASCADE,
    target       TEXT NOT NULL CHECK (target LIKE 'field:%' OR target LIKE 'document:%'),
    kind         TEXT NOT NULL CHECK (kind IN ('value','condition')),
    source_id    TEXT NOT NULL,
    snapshot_id  TEXT NOT NULL,
    quote        TEXT NOT NULL,
    recorded_at  TEXT NOT NULL
);
CREATE INDEX idx_policy_evidence_target ON POLICY_EVIDENCE(policy_id, target);

CREATE TABLE POLICY_REVISION (
    revision_id   TEXT PRIMARY KEY,
    policy_id     TEXT NOT NULL REFERENCES VISA_POLICY(policy_id),
    revision_no   INTEGER NOT NULL CHECK (revision_no >= 1),
    facts_hash    TEXT NOT NULL,
    payload_json  TEXT NOT NULL,
    created_by    TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    UNIQUE (policy_id, revision_no)
);
CREATE TRIGGER policy_revision_no_update BEFORE UPDATE ON POLICY_REVISION
BEGIN
    SELECT RAISE(ABORT, 'POLICY_REVISION is immutable');
END;
CREATE TRIGGER policy_revision_no_delete BEFORE DELETE ON POLICY_REVISION
BEGIN
    SELECT RAISE(ABORT, 'POLICY_REVISION is immutable');
END;

CREATE TABLE POLICY_PUBLICATION (
    policy_id        TEXT PRIMARY KEY REFERENCES VISA_POLICY(policy_id),
    revision_id      TEXT NOT NULL REFERENCES POLICY_REVISION(revision_id),
    status           TEXT NOT NULL CHECK (status IN ('ACTIVE','WITHDRAWN')),
    published_by     TEXT NOT NULL,
    published_at     TEXT NOT NULL,
    verified_at      TEXT NOT NULL,
    withdrawn_by     TEXT,
    withdrawn_at     TEXT,
    withdraw_reason  TEXT,
    CHECK (status = 'ACTIVE' OR (withdrawn_by IS NOT NULL AND withdrawn_at IS NOT NULL
                                 AND withdraw_reason IS NOT NULL))
);
CREATE TRIGGER policy_publication_own_revision_insert BEFORE INSERT ON POLICY_PUBLICATION
WHEN (SELECT policy_id FROM POLICY_REVISION WHERE revision_id = NEW.revision_id) IS NOT NEW.policy_id
BEGIN
    SELECT RAISE(ABORT, 'publication must point at a revision of the same policy');
END;
CREATE TRIGGER policy_publication_own_revision_update BEFORE UPDATE ON POLICY_PUBLICATION
WHEN (SELECT policy_id FROM POLICY_REVISION WHERE revision_id = NEW.revision_id) IS NOT NEW.policy_id
BEGIN
    SELECT RAISE(ABORT, 'publication must point at a revision of the same policy');
END;

CREATE TABLE PUBLICATION_EVENT (
    event_id              TEXT PRIMARY KEY,
    policy_id             TEXT NOT NULL,
    action                TEXT NOT NULL CHECK (action IN ('PUBLISH','REVERIFY','WITHDRAW')),
    revision_id           TEXT,
    previous_revision_id  TEXT,
    actor                 TEXT NOT NULL,
    reason                TEXT,
    detail_json           TEXT,
    at                    TEXT NOT NULL
);
CREATE INDEX idx_publication_event_policy ON PUBLICATION_EVENT(policy_id, at);
CREATE TRIGGER publication_event_no_update BEFORE UPDATE ON PUBLICATION_EVENT
BEGIN
    SELECT RAISE(ABORT, 'PUBLICATION_EVENT is append-only');
END;
CREATE TRIGGER publication_event_no_delete BEFORE DELETE ON PUBLICATION_EVENT
BEGIN
    SELECT RAISE(ABORT, 'PUBLICATION_EVENT is append-only');
END;
