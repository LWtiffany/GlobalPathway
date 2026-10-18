-- Reward rule stacking semantics and promo validity windows.
-- stacking: 'total' (rate includes the card base rate, historical default)
--           'extra' (rate is an incremental layer stacked on top of the base/total layer)
-- valid_until: promotional rules stop applying after this instant (engine filters them).
ALTER TABLE CARD_REWARD_RULE ADD COLUMN IF NOT EXISTS stacking TEXT NOT NULL DEFAULT 'total';
ALTER TABLE CARD_REWARD_RULE ADD COLUMN IF NOT EXISTS valid_until TIMESTAMPTZ;

-- Annual-fee waiver tied to a minimum annual eligible spend
-- (e.g. HSBC EveryMile: subsequent years waived with HKD80,000 annual spend).
ALTER TABLE CARD_FEE ADD COLUMN IF NOT EXISTS waiver_min_annual_spend_hkd NUMERIC;
