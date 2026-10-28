ALTER TABLE CARD_REWARD_RULE
    ADD COLUMN IF NOT EXISTS qualification_type TEXT NOT NULL DEFAULT 'automatic'
        CHECK (qualification_type IN (
            'automatic', 'selected_category', 'payment_method', 'designated_merchant'
        ));

ALTER TABLE CARD_REWARD_RULE
    ADD COLUMN IF NOT EXISTS qualification_value TEXT;

