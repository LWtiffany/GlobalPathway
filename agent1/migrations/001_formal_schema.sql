-- Acquisition and candidates. All timestamps are UTC ISO 8601, all IDs are opaque.
CREATE TABLE sources (
    source_id TEXT PRIMARY KEY NOT NULL,
    name TEXT NOT NULL,
    url TEXT NOT NULL,
    allowed_host TEXT NOT NULL,
    category TEXT NOT NULL,
    country TEXT,
    authority TEXT NOT NULL CHECK(authority IN ('official','partner','third_party')),
    language TEXT NOT NULL DEFAULT 'en',
    owner TEXT NOT NULL,
    access_basis TEXT NOT NULL,
    review_status TEXT NOT NULL DEFAULT 'pending' CHECK(review_status IN ('pending','approved','rejected')),
    enabled INTEGER NOT NULL DEFAULT 0 CHECK(enabled IN (0,1)),
    check_interval_minutes INTEGER NOT NULL CHECK(check_interval_minutes > 0),
    freshness_sla_hours INTEGER NOT NULL CHECK(freshness_sla_hours > 0),
    crawl_config_json TEXT NOT NULL CHECK(json_valid(crawl_config_json) AND json_type(crawl_config_json)='object'),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE source_snapshots (
    snapshot_id TEXT PRIMARY KEY NOT NULL,
    source_id TEXT NOT NULL REFERENCES sources(source_id) ON DELETE RESTRICT,
    content_hash TEXT NOT NULL,
    capture_config_hash TEXT NOT NULL,
    capture_config_json TEXT NOT NULL CHECK(json_valid(capture_config_json) AND json_type(capture_config_json)='object'),
    final_url TEXT NOT NULL,
    html_path TEXT NOT NULL,
    markdown_path TEXT NOT NULL,
    captured_at TEXT NOT NULL,
    UNIQUE(snapshot_id, source_id),
    UNIQUE(source_id, content_hash, capture_config_hash)
);

CREATE TABLE crawl_runs (
    run_id TEXT PRIMARY KEY NOT NULL,
    source_id TEXT NOT NULL REFERENCES sources(source_id) ON DELETE RESTRICT,
    snapshot_id TEXT,
    retry_of TEXT,
    trigger TEXT NOT NULL CHECK(trigger IN ('manual','schedule','retry','test')),
    status TEXT NOT NULL CHECK(status IN ('RUNNING','SUCCEEDED','FAILED','SKIPPED')),
    attempt INTEGER NOT NULL CHECK(attempt > 0),
    started_at TEXT NOT NULL,
    finished_at TEXT,
    retry_after TEXT,
    error_kind TEXT,
    error_message TEXT,
    UNIQUE(run_id, source_id),
    UNIQUE(run_id, snapshot_id, source_id),
    FOREIGN KEY(snapshot_id, source_id) REFERENCES source_snapshots(snapshot_id, source_id) ON DELETE RESTRICT,
    FOREIGN KEY(retry_of, source_id) REFERENCES crawl_runs(run_id, source_id) ON DELETE RESTRICT,
    CHECK(retry_of IS NULL OR retry_of <> run_id),
    CHECK((status='RUNNING' AND finished_at IS NULL) OR (status<>'RUNNING' AND finished_at IS NOT NULL)),
    CHECK(status<>'SUCCEEDED' OR snapshot_id IS NOT NULL)
);

CREATE UNIQUE INDEX one_running_crawl_per_source ON crawl_runs(source_id) WHERE status='RUNNING';
CREATE INDEX crawl_runs_by_source_time ON crawl_runs(source_id, started_at DESC);

CREATE TABLE extraction_runs (
    extraction_id TEXT PRIMARY KEY NOT NULL,
    source_id TEXT NOT NULL,
    crawl_run_id TEXT NOT NULL,
    snapshot_id TEXT NOT NULL,
    method TEXT NOT NULL CHECK(method IN ('llm','rule_fallback','manual_import')),
    model TEXT,
    prompt_version TEXT NOT NULL,
    schema_version TEXT NOT NULL,
    extractor_version TEXT NOT NULL,
    input_manifest_json TEXT NOT NULL CHECK(json_valid(input_manifest_json) AND json_type(input_manifest_json)='object'),
    coverage_status TEXT NOT NULL CHECK(coverage_status IN ('complete','partial','unknown')),
    result_json TEXT CHECK(result_json IS NULL OR json_valid(result_json)),
    status TEXT NOT NULL CHECK(status IN ('RUNNING','SUCCEEDED','FAILED')),
    prompt_tokens INTEGER CHECK(prompt_tokens >= 0),
    completion_tokens INTEGER CHECK(completion_tokens >= 0),
    error_kind TEXT,
    error_message TEXT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    UNIQUE(extraction_id, source_id),
    FOREIGN KEY(crawl_run_id, snapshot_id, source_id) REFERENCES crawl_runs(run_id, snapshot_id, source_id) ON DELETE RESTRICT,
    CHECK(method<>'llm' OR model IS NOT NULL),
    CHECK((status='RUNNING' AND finished_at IS NULL) OR (status<>'RUNNING' AND finished_at IS NOT NULL)),
    CHECK(status<>'SUCCEEDED' OR result_json IS NOT NULL)
);

CREATE INDEX extraction_runs_by_snapshot ON extraction_runs(snapshot_id, started_at DESC);

-- Stable identity, not a published fact. Multiple source pages stay separate until reconciliation.
CREATE TABLE visa_policy (
    policy_id TEXT PRIMARY KEY NOT NULL,
    source_id TEXT NOT NULL REFERENCES sources(source_id) ON DELETE RESTRICT,
    country TEXT NOT NULL,
    policy_type TEXT NOT NULL,
    policy_topic TEXT NOT NULL,
    scope_code TEXT NOT NULL,
    title TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(policy_id, source_id),
    UNIQUE(source_id, country, policy_type, policy_topic, scope_code)
);

CREATE TABLE change_candidates (
    candidate_id TEXT PRIMARY KEY NOT NULL,
    source_id TEXT NOT NULL,
    extraction_id TEXT NOT NULL,
    domain TEXT NOT NULL CHECK(domain IN ('visa_policy','generic_facts','fx_policy','account','credit_card','school')),
    policy_id TEXT,
    base_revision_id TEXT,
    normalized_hash TEXT NOT NULL,
    normalization_version TEXT NOT NULL,
    proposed_payload_json TEXT NOT NULL CHECK(json_valid(proposed_payload_json) AND json_type(proposed_payload_json)='object'),
    diff_json TEXT NOT NULL CHECK(json_valid(diff_json)),
    evidence_json TEXT NOT NULL CHECK(json_valid(evidence_json)),
    gate_result TEXT NOT NULL CHECK(gate_result IN ('PASS','WARNING','ERROR')),
    status TEXT NOT NULL CHECK(status IN ('DRAFT','PENDING_REVIEW','APPROVED','REJECTED','SUPERSEDED','APPLIED','NO_CHANGE')),
    created_at TEXT NOT NULL,
    UNIQUE(candidate_id, policy_id),
    UNIQUE(candidate_id, source_id),
    FOREIGN KEY(extraction_id, source_id) REFERENCES extraction_runs(extraction_id, source_id) ON DELETE RESTRICT,
    FOREIGN KEY(policy_id, source_id) REFERENCES visa_policy(policy_id, source_id) ON DELETE RESTRICT,
    FOREIGN KEY(base_revision_id, policy_id) REFERENCES visa_policy_revision(revision_id, policy_id) ON DELETE RESTRICT,
    CHECK((domain='visa_policy' AND policy_id IS NOT NULL) OR (domain<>'visa_policy' AND policy_id IS NULL)),
    CHECK(policy_id IS NOT NULL OR base_revision_id IS NULL)
);

CREATE INDEX candidates_by_status ON change_candidates(status, created_at);

CREATE TABLE review_tasks (
    review_id TEXT PRIMARY KEY NOT NULL,
    candidate_id TEXT NOT NULL REFERENCES change_candidates(candidate_id) ON DELETE RESTRICT,
    reason TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('OPEN','APPROVED','REJECTED','CANCELLED')),
    assigned_to TEXT,
    decided_by TEXT,
    decided_at TEXT,
    decision_note TEXT,
    created_at TEXT NOT NULL,
    CHECK((status='OPEN' AND decided_at IS NULL AND decided_by IS NULL)
       OR (status<>'OPEN' AND decided_at IS NOT NULL AND decided_by IS NOT NULL AND decision_note IS NOT NULL))
);

CREATE UNIQUE INDEX one_open_review_per_candidate ON review_tasks(candidate_id) WHERE status='OPEN';

CREATE TABLE quality_issues (
    issue_id TEXT PRIMARY KEY NOT NULL,
    source_id TEXT NOT NULL REFERENCES sources(source_id) ON DELETE RESTRICT,
    crawl_run_id TEXT,
    extraction_id TEXT,
    candidate_id TEXT,
    field_path TEXT,
    check_name TEXT NOT NULL,
    severity TEXT NOT NULL CHECK(severity IN ('ERROR','WARNING')),
    reason TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('OPEN','RESOLVED','WAIVED','SUPERSEDED')),
    resolved_by TEXT,
    resolved_at TEXT,
    resolution TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY(crawl_run_id, source_id) REFERENCES crawl_runs(run_id, source_id) ON DELETE RESTRICT,
    FOREIGN KEY(extraction_id, source_id) REFERENCES extraction_runs(extraction_id, source_id) ON DELETE RESTRICT,
    FOREIGN KEY(candidate_id, source_id) REFERENCES change_candidates(candidate_id, source_id) ON DELETE RESTRICT,
    CHECK(crawl_run_id IS NOT NULL OR extraction_id IS NOT NULL OR candidate_id IS NOT NULL),
    CHECK((status='OPEN' AND resolved_at IS NULL AND resolved_by IS NULL)
       OR (status<>'OPEN' AND resolved_at IS NOT NULL AND resolved_by IS NOT NULL AND resolution IS NOT NULL))
);

CREATE INDEX quality_issues_by_status ON quality_issues(status, source_id);

-- Typed content. No current pointer is stored inside the revision itself.
CREATE TABLE visa_policy_revision (
    revision_id TEXT PRIMARY KEY NOT NULL,
    policy_id TEXT NOT NULL,
    source_id TEXT NOT NULL,
    revision_no INTEGER NOT NULL CHECK(revision_no > 0),
    candidate_id TEXT NOT NULL UNIQUE,
    supersedes_revision_id TEXT,
    effective_from TEXT,
    effective_to TEXT,
    cas_required INTEGER CHECK(cas_required IN (0,1)),
    i20_required INTEGER CHECK(i20_required IN (0,1)),
    funds_tuition_required INTEGER CHECK(funds_tuition_required IN (0,1)),
    funds_tuition_unpaid NUMERIC CHECK(funds_tuition_unpaid IS NULL OR (typeof(funds_tuition_unpaid) IN ('integer','real') AND funds_tuition_unpaid >= 0)),
    funds_tuition_currency TEXT CHECK(funds_tuition_currency IS NULL OR funds_tuition_currency GLOB '[A-Z][A-Z][A-Z]'),
    funds_living_monthly NUMERIC CHECK(funds_living_monthly IS NULL OR (typeof(funds_living_monthly) IN ('integer','real') AND funds_living_monthly >= 0)),
    funds_living_currency TEXT CHECK(funds_living_currency IS NULL OR funds_living_currency GLOB '[A-Z][A-Z][A-Z]'),
    funds_living_months INTEGER CHECK(typeof(funds_living_months)='null' OR (typeof(funds_living_months)='integer' AND funds_living_months > 0)),
    funds_holding_days INTEGER CHECK(typeof(funds_holding_days)='null' OR (typeof(funds_holding_days)='integer' AND funds_holding_days > 0)),
    funds_doc_max_age_days INTEGER CHECK(typeof(funds_doc_max_age_days)='null' OR (typeof(funds_doc_max_age_days)='integer' AND funds_doc_max_age_days > 0)),
    funds_owner_allowed TEXT,
    english_level TEXT,
    tb_test_required INTEGER CHECK(tb_test_required IN (0,1)),
    health_surcharge NUMERIC CHECK(health_surcharge IS NULL OR (typeof(health_surcharge) IN ('integer','real') AND health_surcharge >= 0)),
    apply_window_days INTEGER CHECK(typeof(apply_window_days)='null' OR (typeof(apply_window_days)='integer' AND apply_window_days > 0)),
    processing_days INTEGER CHECK(typeof(processing_days)='null' OR (typeof(processing_days)='integer' AND processing_days > 0)),
    visa_fee NUMERIC CHECK(visa_fee IS NULL OR (typeof(visa_fee) IN ('integer','real') AND visa_fee >= 0)),
    fee_currency TEXT CHECK(fee_currency IS NULL OR fee_currency GLOB '[A-Z][A-Z][A-Z]'),
    health_surcharge_currency TEXT CHECK(health_surcharge_currency IS NULL OR health_surcharge_currency GLOB '[A-Z][A-Z][A-Z]'),
    applies_to_city TEXT,
    conditions_json TEXT NOT NULL CHECK(json_valid(conditions_json) AND json_type(conditions_json)='array'),
    remark TEXT,
    normalized_hash TEXT NOT NULL,
    normalization_version TEXT NOT NULL,
    accepted_by TEXT NOT NULL,
    accepted_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(revision_id, policy_id),
    UNIQUE(revision_id, source_id),
    UNIQUE(policy_id, revision_no),
    FOREIGN KEY(policy_id, source_id) REFERENCES visa_policy(policy_id, source_id) ON DELETE RESTRICT,
    FOREIGN KEY(candidate_id, policy_id) REFERENCES change_candidates(candidate_id, policy_id) ON DELETE RESTRICT,
    FOREIGN KEY(supersedes_revision_id, policy_id) REFERENCES visa_policy_revision(revision_id, policy_id) ON DELETE RESTRICT,
    CHECK(supersedes_revision_id IS NULL OR supersedes_revision_id <> revision_id),
    CHECK(effective_from IS NULL OR effective_to IS NULL OR effective_from <= effective_to),
    CHECK(funds_tuition_unpaid IS NULL OR funds_tuition_currency IS NOT NULL),
    CHECK(funds_living_monthly IS NULL OR funds_living_currency IS NOT NULL),
    CHECK(visa_fee IS NULL OR fee_currency IS NOT NULL),
    CHECK(health_surcharge IS NULL OR health_surcharge_currency IS NOT NULL)
);

CREATE TABLE document (
    doc_id TEXT PRIMARY KEY NOT NULL,
    code TEXT NOT NULL UNIQUE,
    name_zh TEXT NOT NULL,
    aliases_json TEXT NOT NULL DEFAULT '[]' CHECK(json_valid(aliases_json) AND json_type(aliases_json)='array'),
    created_at TEXT NOT NULL
);

CREATE TABLE policy_document (
    revision_id TEXT NOT NULL REFERENCES visa_policy_revision(revision_id) ON DELETE RESTRICT,
    doc_id TEXT NOT NULL REFERENCES document(doc_id) ON DELETE RESTRICT,
    requirement_key TEXT NOT NULL,
    required INTEGER CHECK(required IN (0,1)),
    applicability_status TEXT NOT NULL CHECK(applicability_status IN ('unconditional','conditional','unknown')),
    condition_json TEXT CHECK(condition_json IS NULL OR (json_valid(condition_json) AND json_type(condition_json)='object')),
    PRIMARY KEY(revision_id, doc_id, requirement_key),
    CHECK((applicability_status='conditional' AND condition_json IS NOT NULL AND condition_json <> '{}')
       OR (applicability_status='unconditional' AND condition_json IS NULL)
       OR (applicability_status='unknown' AND condition_json IS NULL AND required IS NULL))
);

CREATE TABLE policy_evidence (
    evidence_id TEXT PRIMARY KEY NOT NULL,
    revision_id TEXT NOT NULL,
    source_id TEXT NOT NULL,
    snapshot_id TEXT NOT NULL,
    field_path TEXT,
    doc_id TEXT,
    requirement_key TEXT,
    evidence_quote TEXT NOT NULL CHECK(length(trim(evidence_quote)) > 0),
    locator_json TEXT CHECK(locator_json IS NULL OR json_valid(locator_json)),
    note TEXT,
    FOREIGN KEY(revision_id, source_id) REFERENCES visa_policy_revision(revision_id, source_id) ON DELETE RESTRICT,
    FOREIGN KEY(snapshot_id, source_id) REFERENCES source_snapshots(snapshot_id, source_id) ON DELETE RESTRICT,
    FOREIGN KEY(revision_id, doc_id, requirement_key) REFERENCES policy_document(revision_id, doc_id, requirement_key) ON DELETE RESTRICT,
    CHECK((field_path IS NOT NULL AND doc_id IS NULL AND requirement_key IS NULL)
       OR (field_path IS NULL AND doc_id IS NOT NULL AND requirement_key IS NOT NULL))
);

CREATE INDEX evidence_by_revision ON policy_evidence(revision_id);

CREATE TABLE policy_publication (
    policy_id TEXT PRIMARY KEY NOT NULL,
    source_id TEXT NOT NULL,
    revision_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('ACTIVE','WITHDRAWN')),
    published_by TEXT NOT NULL,
    published_at TEXT NOT NULL,
    last_verified_at TEXT NOT NULL,
    verification_run_id TEXT NOT NULL,
    withdrawal_reason TEXT,
    withdrawn_by TEXT,
    withdrawn_at TEXT,
    FOREIGN KEY(revision_id, policy_id) REFERENCES visa_policy_revision(revision_id, policy_id) ON DELETE RESTRICT,
    FOREIGN KEY(policy_id, source_id) REFERENCES visa_policy(policy_id, source_id) ON DELETE RESTRICT,
    FOREIGN KEY(verification_run_id, source_id) REFERENCES crawl_runs(run_id, source_id) ON DELETE RESTRICT,
    CHECK((status='ACTIVE' AND withdrawal_reason IS NULL AND withdrawn_by IS NULL AND withdrawn_at IS NULL)
       OR (status='WITHDRAWN' AND withdrawal_reason IS NOT NULL AND withdrawn_by IS NOT NULL AND withdrawn_at IS NOT NULL))
);

CREATE TABLE audit_events (
    event_id TEXT PRIMARY KEY NOT NULL,
    transaction_id TEXT NOT NULL,
    source_id TEXT REFERENCES sources(source_id) ON DELETE RESTRICT,
    candidate_id TEXT REFERENCES change_candidates(candidate_id) ON DELETE RESTRICT,
    review_id TEXT REFERENCES review_tasks(review_id) ON DELETE RESTRICT,
    policy_id TEXT REFERENCES visa_policy(policy_id) ON DELETE RESTRICT,
    from_revision_id TEXT,
    to_revision_id TEXT,
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    reason TEXT NOT NULL,
    detail_json TEXT NOT NULL CHECK(json_valid(detail_json)),
    occurred_at TEXT NOT NULL,
    FOREIGN KEY(from_revision_id, policy_id) REFERENCES visa_policy_revision(revision_id, policy_id) ON DELETE RESTRICT,
    FOREIGN KEY(to_revision_id, policy_id) REFERENCES visa_policy_revision(revision_id, policy_id) ON DELETE RESTRICT,
    CHECK(policy_id IS NOT NULL OR (from_revision_id IS NULL AND to_revision_id IS NULL))
);

CREATE INDEX audit_by_policy_time ON audit_events(policy_id, occurred_at);
CREATE INDEX audit_by_transaction ON audit_events(transaction_id);

-- Structural protection; publication authorization and semantic gates are the next stage.
CREATE TRIGGER snapshot_no_update BEFORE UPDATE ON source_snapshots
BEGIN SELECT RAISE(ABORT, 'snapshots are immutable'); END;

CREATE TRIGGER revision_no_update BEFORE UPDATE ON visa_policy_revision
BEGIN SELECT RAISE(ABORT, 'accepted revisions are immutable'); END;

CREATE TRIGGER revision_no_delete BEFORE DELETE ON visa_policy_revision
BEGIN SELECT RAISE(ABORT, 'accepted revisions must be archived, not deleted'); END;

CREATE TRIGGER audit_no_update BEFORE UPDATE ON audit_events
BEGIN SELECT RAISE(ABORT, 'audit events are append-only'); END;

CREATE TRIGGER audit_no_delete BEFORE DELETE ON audit_events
BEGIN SELECT RAISE(ABORT, 'audit events are append-only'); END;
