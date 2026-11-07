-- Curated, customer-safe credit-card domain knowledge.
-- Product pricing and eligibility remain in CREDIT_CARD child tables.  This
-- table is only for general servicing knowledge that is not card-specific.

CREATE TABLE IF NOT EXISTS CARD_KNOWLEDGE_ENTRY (
    knowledge_id TEXT PRIMARY KEY,
    market TEXT NOT NULL CHECK (market IN ('CN-HK', 'UK', 'US', 'GENERAL')),
    topic TEXT NOT NULL CHECK (topic IN (
        'repayment', 'rewards', 'overseas_use', 'lost_or_stolen',
        'transaction_dispute', 'instalment'
    )),
    title TEXT NOT NULL,
    customer_answer TEXT NOT NULL,
    source_url TEXT NOT NULL,
    verified_at TIMESTAMPTZ NOT NULL,
    effective_from DATE,
    effective_to DATE,
    status TEXT NOT NULL DEFAULT 'ACTIVE' CHECK (status IN ('ACTIVE', 'PENDING_REVIEW', 'WITHDRAWN')),
    CHECK (effective_to IS NULL OR effective_from IS NULL OR effective_to >= effective_from)
);

CREATE INDEX IF NOT EXISTS idx_card_knowledge_market_topic
    ON CARD_KNOWLEDGE_ENTRY(market, topic, status);
