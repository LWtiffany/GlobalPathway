-- Additive decision architecture for HSBC family and pre-departure account planning.
-- Existing product/rule rows remain valid legacy facts. These rows describe service
-- paths and their scope; they do not grant eligibility or guarantee approval.

CREATE TABLE IF NOT EXISTS account_service_policy (
    policy_id TEXT PRIMARY KEY,
    service_type TEXT NOT NULL CHECK (service_type IN
        ('family_banking', 'overseas_opening', 'predeparture_planning', 'relationship_manager_handoff')),
    bank_code TEXT NOT NULL,
    origin_market TEXT,
    destination_market TEXT NOT NULL,
    owner_role TEXT NOT NULL CHECK (owner_role IN ('parent', 'applicant', 'guardian', 'household')),
    beneficiary_role TEXT NOT NULL CHECK (beneficiary_role IN ('parent', 'applicant', 'child', 'household')),
    required_relationship_status TEXT NOT NULL CHECK (required_relationship_status IN
        ('stated', 'bank_verified', 'not_required')),
    eligibility_status TEXT NOT NULL CHECK (eligibility_status IN
        ('possible_route', 'requires_bank_confirmation', 'not_applicable')),
    action_code TEXT NOT NULL,
    description TEXT NOT NULL,
    official_url TEXT NOT NULL,
    verified_at TIMESTAMPTZ NOT NULL,
    effective_from DATE,
    effective_to DATE,
    UNIQUE (service_type, origin_market, destination_market, owner_role, beneficiary_role)
);

CREATE TABLE IF NOT EXISTS account_decision_path (
    path_id TEXT PRIMARY KEY,
    policy_id TEXT REFERENCES account_service_policy(policy_id),
    account_id TEXT REFERENCES account_product(account_id),
    origin_market TEXT,
    destination_market TEXT NOT NULL,
    applicant_location TEXT,
    subject_role TEXT NOT NULL CHECK (subject_role IN ('applicant', 'parent', 'guardian', 'household')),
    relationship_holder_role TEXT CHECK (relationship_holder_role IN ('applicant', 'parent', 'guardian', 'household')),
    path_status TEXT NOT NULL CHECK (path_status IN ('published', 'requires_bank_confirmation', 'withdrawn')),
    predeparture_stage TEXT NOT NULL CHECK (predeparture_stage IN
        ('not_supported', 'application_only', 'application_and_review', 'bank_confirmation')),
    description TEXT NOT NULL,
    source_url TEXT NOT NULL,
    verified_at TIMESTAMPTZ NOT NULL,
    UNIQUE (path_id)
);
CREATE INDEX IF NOT EXISTS idx_account_decision_path_scope
    ON account_decision_path(destination_market, origin_market, applicant_location, path_status);

CREATE TABLE IF NOT EXISTS account_decision_condition (
    condition_id TEXT PRIMARY KEY,
    path_id TEXT NOT NULL REFERENCES account_decision_path(path_id) ON DELETE CASCADE,
    group_code TEXT NOT NULL,
    field_code TEXT NOT NULL,
    operator TEXT NOT NULL CHECK (operator IN ('eq', 'neq', 'gte', 'lte', 'in', 'present', 'bank_confirm')),
    value_text TEXT,
    value_numeric NUMERIC,
    currency TEXT,
    required BOOLEAN NOT NULL DEFAULT TRUE,
    description TEXT NOT NULL,
    source_url TEXT NOT NULL,
    verified_at TIMESTAMPTZ NOT NULL,
    CHECK (value_text IS NOT NULL OR value_numeric IS NOT NULL OR operator IN ('present', 'bank_confirm'))
);
CREATE INDEX IF NOT EXISTS idx_account_decision_condition_path
    ON account_decision_condition(path_id, group_code);

CREATE TABLE IF NOT EXISTS account_decision_evidence (
    evidence_id TEXT PRIMARY KEY,
    path_id TEXT REFERENCES account_decision_path(path_id) ON DELETE CASCADE,
    condition_id TEXT REFERENCES account_decision_condition(condition_id) ON DELETE CASCADE,
    doc_id TEXT,
    chunk_id TEXT,
    claim_scope TEXT NOT NULL CHECK (claim_scope IN ('route', 'condition', 'limitation', 'action')),
    quote TEXT NOT NULL,
    source_url TEXT NOT NULL,
    verified_at TIMESTAMPTZ NOT NULL,
    CHECK (path_id IS NOT NULL OR condition_id IS NOT NULL)
);
CREATE INDEX IF NOT EXISTS idx_account_decision_evidence_path
    ON account_decision_evidence(path_id, claim_scope);

INSERT INTO account_service_policy
(policy_id, service_type, bank_code, origin_market, destination_market, owner_role,
 beneficiary_role, required_relationship_status, eligibility_status, action_code,
 description, official_url, verified_at)
VALUES
('svc_hk_family_uk_predeparture', 'predeparture_planning', 'HSBC', 'CN-HK', 'UK',
 'parent', 'child', 'stated', 'requires_bank_confirmation', 'verify_with_relationship_manager',
 '香港家庭关系可作为核对英国出发前开户协助的线索，但不自动赋予孩子英国账户资格或费用权益。',
 'https://www.hsbc.com.hk/premier/global-banking/', '2026-11-04T00:00:00+00:00'),
('svc_hk_family_overseas_opening', 'overseas_opening', 'HSBC', 'CN-HK', 'UK',
 'parent', 'applicant', 'bank_verified', 'possible_route', 'check_destination_channel',
 '可核对境外申请入口；目的地实体的申请资格、材料和审批规则优先。',
 'https://www.hsbc.co.uk/international/apply-for-a-uk-account/', '2026-11-04T00:00:00+00:00'),
('svc_crossborder_handoff', 'relationship_manager_handoff', 'HSBC', NULL, 'UK',
 'parent', 'applicant', 'stated', 'requires_bank_confirmation', 'prepare_handoff_summary',
 '生成给客户经理的核对摘要，不代表已经预约、转介或提交申请。',
 'https://internationalservices.hsbc.com/overseas-account-opening/', '2026-11-04T00:00:00+00:00')
ON CONFLICT (policy_id) DO UPDATE SET
    description=EXCLUDED.description, official_url=EXCLUDED.official_url,
    verified_at=EXCLUDED.verified_at, eligibility_status=EXCLUDED.eligibility_status;

INSERT INTO account_decision_path
(path_id, policy_id, account_id, origin_market, destination_market, applicant_location,
 subject_role, relationship_holder_role, path_status, predeparture_stage, description,
 source_url, verified_at)
VALUES
('path_uk_bank_abroad_hk_parent', 'svc_hk_family_overseas_opening', 'acct_uk_bank', 'CN-HK', 'UK',
 NULL, 'applicant', 'parent', 'published', 'application_only',
 '英国普通账户存在境外申请入口；具体所在地、身份和地址条件仍须按申请页面确认。',
 'https://www.hsbc.co.uk/international/apply-for-a-uk-account/', '2026-11-04T00:00:00+00:00'),
('path_uk_family_rm_check', 'svc_hk_family_uk_predeparture', NULL, 'CN-HK', 'UK',
 NULL, 'applicant', 'parent', 'requires_bank_confirmation', 'bank_confirmation',
 '家庭服务适用性、可申请产品、收卡和激活安排由银行关系经理确认。',
 'https://www.hsbc.com.hk/premier/global-banking/', '2026-11-04T00:00:00+00:00'),
('path_uk_handoff_summary', 'svc_crossborder_handoff', NULL, NULL, 'UK',
 NULL, 'applicant', 'parent', 'requires_bank_confirmation', 'bank_confirmation',
 '客户经理交接只输出已知事实、目标日期和待确认问题，不承诺办理结果。',
 'https://internationalservices.hsbc.com/overseas-account-opening/', '2026-11-04T00:00:00+00:00')
ON CONFLICT (path_id) DO UPDATE SET
    description=EXCLUDED.description, source_url=EXCLUDED.source_url,
    verified_at=EXCLUDED.verified_at, path_status=EXCLUDED.path_status;

INSERT INTO account_decision_condition
(condition_id, path_id, group_code, field_code, operator, value_text, required, description,
 source_url, verified_at)
VALUES
('cond_uk_bank_abroad_location', 'path_uk_bank_abroad_hk_parent', 'required',
 'application_location', 'bank_confirm', NULL, TRUE,
 '申请所在地及可用渠道须由英国申请页面确认。',
 'https://www.hsbc.co.uk/international/apply-for-a-uk-account/', '2026-11-04T00:00:00+00:00'),
('cond_uk_family_relationship', 'path_uk_family_rm_check', 'required',
 'parent_relationship', 'bank_confirm', NULL, TRUE,
 '家长关系陈述不是孩子资格，须由银行确认适用服务。',
 'https://www.hsbc.com.hk/premier/global-banking/', '2026-11-04T00:00:00+00:00'),
('cond_uk_applicant_identity', 'path_uk_family_rm_check', 'required',
 'applicant_identity', 'present', NULL, TRUE,
 '开户人身份必须明确为孩子本人。',
 'https://internationalservices.hsbc.com/overseas-account-opening/', '2026-11-04T00:00:00+00:00')
ON CONFLICT (condition_id) DO NOTHING;

INSERT INTO account_decision_evidence
(evidence_id, path_id, condition_id, doc_id, chunk_id, claim_scope, quote, source_url, verified_at)
VALUES
('evidence_uk_family_route', 'path_uk_family_rm_check', NULL,
 'hsbc_hk_family_education_audit_20261104', 'hsbc_hk_family_education_audit_20261104#002',
 'route', '官方资料提到可为子女核对海外账户安排；当地实体条件和适用服务仍需确认。',
 'https://www.hsbc.com.hk/premier/global-banking/', '2026-11-04T00:00:00+00:00'),
('evidence_uk_abroad_route', 'path_uk_bank_abroad_hk_parent', NULL,
 'hsbc_uk_predeparture_audit_20261104', 'hsbc_uk_predeparture_audit_20261104#004',
 'route', '英国账户可从多个境外地点申请，但需按所选账户和所在地核对资格。',
 'https://www.hsbc.co.uk/international/apply-for-a-uk-account/', '2026-11-04T00:00:00+00:00'),
('evidence_family_limitation', 'path_uk_family_rm_check', 'cond_uk_family_relationship',
 'hsbc_hk_family_education_audit_20261104', 'hsbc_hk_family_education_audit_20261104#007',
 'limitation', '目的地地区可能要求额外资料，服务安排会因当地产品和规则而变化。',
 'https://www.hsbc.com.hk/premier/global-banking/', '2026-11-04T00:00:00+00:00')
ON CONFLICT (evidence_id) DO UPDATE SET
    quote=EXCLUDED.quote, source_url=EXCLUDED.source_url, verified_at=EXCLUDED.verified_at;
