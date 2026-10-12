-- Per-source HTTP validation and section-level extraction reuse.
CREATE TABLE source_check_state (
    source_id TEXT PRIMARY KEY REFERENCES sources(source_id) ON DELETE RESTRICT,
    etag TEXT,
    last_modified TEXT,
    raw_body_hash TEXT,
    cache_epoch TEXT NOT NULL DEFAULT 'initial',
    last_extraction_profile TEXT,
    snapshot_id TEXT,
    last_checked_at TEXT,
    last_complete_at TEXT,
    last_full_at TEXT,
    next_check_at TEXT,
    last_result TEXT CHECK(last_result IN ('NOT_MODIFIED','BODY_UNCHANGED','CHANGED','FAILED')),
    FOREIGN KEY(snapshot_id, source_id) REFERENCES source_snapshots(snapshot_id, source_id) ON DELETE RESTRICT
);

CREATE TABLE snapshot_sections (
    snapshot_id TEXT NOT NULL,
    source_id TEXT NOT NULL,
    section_key TEXT NOT NULL,
    heading TEXT NOT NULL,
    ordinal INTEGER NOT NULL CHECK(ordinal >= 0),
    content_hash TEXT NOT NULL,
    start_offset INTEGER NOT NULL CHECK(start_offset >= 0),
    end_offset INTEGER NOT NULL CHECK(end_offset > start_offset),
    PRIMARY KEY(snapshot_id, section_key),
    FOREIGN KEY(snapshot_id, source_id) REFERENCES source_snapshots(snapshot_id, source_id) ON DELETE RESTRICT,
    UNIQUE(snapshot_id, ordinal)
);

CREATE TABLE section_extractions (
    source_id TEXT NOT NULL REFERENCES sources(source_id) ON DELETE RESTRICT,
    section_hash TEXT NOT NULL,
    extraction_profile TEXT NOT NULL,
    result_json TEXT NOT NULL CHECK(json_valid(result_json) AND json_type(result_json)='object'),
    coverage_status TEXT NOT NULL CHECK(coverage_status IN ('complete','partial')),
    created_at TEXT NOT NULL,
    PRIMARY KEY(source_id, section_hash, extraction_profile)
);

CREATE TABLE source_check_events (
    check_id TEXT PRIMARY KEY NOT NULL,
    source_id TEXT NOT NULL REFERENCES sources(source_id) ON DELETE RESTRICT,
    result TEXT NOT NULL CHECK(result IN ('NOT_DUE','NOT_MODIFIED','BODY_UNCHANGED','CHANGED','FAILED')),
    snapshot_id TEXT,
    checked_at TEXT NOT NULL,
    detail_json TEXT NOT NULL CHECK(json_valid(detail_json)),
    FOREIGN KEY(snapshot_id, source_id) REFERENCES source_snapshots(snapshot_id, source_id) ON DELETE RESTRICT
);

CREATE INDEX check_events_by_source ON source_check_events(source_id, checked_at DESC);

CREATE TABLE source_section_dependencies (
    source_id TEXT NOT NULL REFERENCES sources(source_id) ON DELETE RESTRICT,
    section_key TEXT NOT NULL,
    depends_on_key TEXT NOT NULL,
    reason TEXT NOT NULL,
    PRIMARY KEY(source_id, section_key, depends_on_key),
    CHECK(section_key <> depends_on_key)
);

-- Accepted source HTML must not be overwritten after evidence has been derived from it.
CREATE TRIGGER snapshot_section_no_update BEFORE UPDATE ON snapshot_sections
BEGIN SELECT RAISE(ABORT, 'snapshot sections are immutable'); END;

CREATE TRIGGER section_extraction_no_update BEFORE UPDATE ON section_extractions
BEGIN SELECT RAISE(ABORT, 'section extractions are immutable'); END;
