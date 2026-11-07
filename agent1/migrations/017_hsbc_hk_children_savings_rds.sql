-- Add the verified HSBC HK child account to the existing shared RDS account schema.
-- The existing catalog uses acct_* ids and ACCOUNT_ELIGIBILITY_RULE.

INSERT INTO ACCOUNT_PRODUCT
    (account_id, product_code, product_name, market, account_type, customer_segment,
     primary_currency, product_summary, dedicated_student_product, product_status,
     official_url, data_status, effective_from, effective_to, last_verified_at)
VALUES
    ('acct_hk_children_savings', 'HK_CHILDREN_SAVINGS', 'HSBC Children Savings Account',
     'HK', 'savings', 'general', 'HKD',
     'A Hong Kong dollar savings account for children under 18.',
     0, 'ACTIVE', 'https://www.hsbc.com.hk/accounts/products/savings/children/',
     'verified', NULL, NULL, '2026-10-28T00:00:00+08:00')
ON CONFLICT (account_id) DO NOTHING;

INSERT INTO ACCOUNT_OPENING_ROUTE
    (route_id, account_id, route_name, application_channel, applicant_location,
     identity_document_type, can_apply_before_arrival, branch_visit_required,
     activation_location, processing_time_note, additional_notes, source_url, last_verified_at)
VALUES
    ('route_hk_child_branch', 'acct_hk_children_savings',
     'Children Savings Account branch application', 'branch', 'Hong Kong',
     'Child identity document or birth certificate plus guardian identity document',
     0, 1, 'Hong Kong', NULL,
     'Children aged 11 to 17 attend with a parent or guardian; for children under 11, '
     'the parent or guardian opens the account for the child in the adult name.',
     'https://www.hsbc.com.hk/accounts/products/savings/children/',
     '2026-10-28T00:00:00+08:00')
ON CONFLICT (route_id) DO NOTHING;

INSERT INTO ACCOUNT_ELIGIBILITY_RULE
    (eligibility_rule_id, account_id, route_id, rule_type, operator, value_text,
     value_numeric, currency, required, description, source_url, last_verified_at)
VALUES
    ('elig_hk_child_max_age', 'acct_hk_children_savings', NULL,
     'maximum_age', 'lte', NULL, 17, NULL, 1,
     'The child account applicant must be under 18.',
     'https://www.hsbc.com.hk/accounts/products/savings/children/',
     '2026-10-28T00:00:00+08:00'),
    ('elig_hk_child_guardian', 'acct_hk_children_savings', 'route_hk_child_branch',
     'other', 'eq', 'guardian_present', NULL, NULL, 1,
     'A parent or guardian accompanies an applicant aged 11 to 17 to the branch.',
     'https://www.hsbc.com.hk/accounts/products/savings/children/',
     '2026-10-28T00:00:00+08:00')
ON CONFLICT (eligibility_rule_id) DO NOTHING;

INSERT INTO ACCOUNT_REQUIRED_DOCUMENT
    (requirement_id, account_id, route_id, document_type, document_name, required,
     alternative_group, condition_note, validity_note, source_url, last_verified_at)
VALUES
    ('doc_hk_child_identity', 'acct_hk_children_savings', 'route_hk_child_branch',
     'identity', 'Child identity document; a birth certificate may be used for a child under 11',
     1, 'child_identity', NULL, NULL,
     'https://www.hsbc.com.hk/accounts/products/savings/children/',
     '2026-10-28T00:00:00+08:00'),
    ('doc_hk_child_guardian_identity', 'acct_hk_children_savings', 'route_hk_child_branch',
     'guardian_identity', 'Parent or guardian identity document', 1, NULL, NULL, NULL,
     'https://www.hsbc.com.hk/accounts/products/savings/children/',
     '2026-10-28T00:00:00+08:00')
ON CONFLICT (requirement_id) DO NOTHING;

INSERT INTO ACCOUNT_FEE
    (fee_id, account_id, fee_type, amount, rate, currency, minimum_amount,
     maximum_amount, waiver_available, waiver_condition, description,
     source_url, last_verified_at)
VALUES
    ('fee_hk_child_monthly', 'acct_hk_children_savings', 'monthly_fee', 0, NULL,
     'HKD', NULL, NULL, 0, NULL, 'No monthly account fee.',
     'https://www.hsbc.com.hk/accounts/products/savings/children/',
     '2026-10-28T00:00:00+08:00')
ON CONFLICT (fee_id) DO NOTHING;
