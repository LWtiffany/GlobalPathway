-- Product composition/dependency graph used by the account customer-service agent.
-- These are factual product relationships, separate from user eligibility rules.

CREATE TABLE IF NOT EXISTS account_product_relation (
    relation_id       TEXT PRIMARY KEY,
    source_account_id TEXT NOT NULL REFERENCES account_product(account_id) ON DELETE CASCADE,
    target_account_id TEXT NOT NULL REFERENCES account_product(account_id) ON DELETE CASCADE,
    relation_type     TEXT NOT NULL CHECK (relation_type IN
        ('requires_relationship', 'companion_to', 'tier_of', 'alternative_to')),
    description       TEXT NOT NULL,
    source_url        TEXT NOT NULL,
    last_verified_at  DATE NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_account_relation_source
    ON account_product_relation(source_account_id, relation_type);
CREATE INDEX IF NOT EXISTS idx_account_relation_target
    ON account_product_relation(target_account_id, relation_type);
