-- Official source documents for HSBC deposit accounts (2026-10-29).
-- Full fetched page text lives in OSS (see oss_key); the RDS keeps metadata
-- plus per-chunk Chinese summaries that feed the writer's retrieval step.
-- Chunk text is derived from the archived page; numbers are kept verbatim.

CREATE TABLE IF NOT EXISTS ACCOUNT_SOURCE_DOCUMENT (
    doc_id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL,          -- ACCOUNT_PRODUCT.account_id (physical id)
    market TEXT NOT NULL CHECK (market IN ('UK', 'US', 'CN-HK')),
    title TEXT NOT NULL,
    source_url TEXT NOT NULL,
    doc_type TEXT NOT NULL CHECK (doc_type IN ('product_page', 'faq', 'fee_schedule', 'guide')),
    oss_bucket TEXT NOT NULL,
    oss_key TEXT NOT NULL,
    content_sha256 TEXT NOT NULL,      -- sha256 of the archived markdown in OSS
    chunk_count INTEGER NOT NULL,
    fetched_at DATE NOT NULL,
    verified_at DATE NOT NULL
);

CREATE TABLE IF NOT EXISTS ACCOUNT_DOC_CHUNK (
    chunk_id TEXT PRIMARY KEY,
    doc_id TEXT NOT NULL REFERENCES ACCOUNT_SOURCE_DOCUMENT(doc_id),
    account_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    heading TEXT NOT NULL,
    content TEXT NOT NULL,             -- Chinese digest of that section, numbers verbatim
    content_sha256 TEXT NOT NULL,
    UNIQUE (doc_id, seq)
);
CREATE INDEX IF NOT EXISTS idx_account_doc_chunk_account ON ACCOUNT_DOC_CHUNK(account_id);
