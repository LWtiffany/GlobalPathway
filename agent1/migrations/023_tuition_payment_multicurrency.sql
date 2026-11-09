-- 021_tuition_payment_multicurrency.sql
-- Explicit multi-currency / local-currency payment channels (Flywire, Convera, …).

ALTER TABLE TUITION_PAYMENT_DETAIL
    ADD COLUMN IF NOT EXISTS supports_multi_currency INTEGER
        CHECK (supports_multi_currency IS NULL OR supports_multi_currency IN (0, 1));

ALTER TABLE TUITION_PAYMENT_DETAIL
    ADD COLUMN IF NOT EXISTS multi_currency_channels JSONB NOT NULL DEFAULT '[]'::jsonb;

ALTER TABLE TUITION_PAYMENT_DETAIL
    ADD COLUMN IF NOT EXISTS multi_currency_note TEXT;
