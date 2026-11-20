-- 024_fx_study_abroad_seed.sql
-- Curated CN study-abroad FX facts for Agent2 FAQ coverage.
-- Idempotent upserts against existing FX_POLICY rows (tuition / living).
-- Prefer applying via scripts/seed_fx_study_abroad.py so DOCUMENT ids stay consistent.

-- Policy field backfill (matched by purpose_code; does not invent new policy rows).
UPDATE FX_POLICY
SET annual_quota_amount = COALESCE(annual_quota_amount, 50000),
    annual_quota_currency = COALESCE(annual_quota_currency, 'USD'),
    quota_reset = COALESCE(quota_reset, 'calendar_year'),
    over_quota_allowed = COALESCE(over_quota_allowed, 1),
    remitter_relation = COALESCE(remitter_relation, 'immediate_family'),
    can_use_family_quota = COALESCE(can_use_family_quota, 1),
    tuition_direct_to_school = COALESCE(tuition_direct_to_school, 1),
    remark = COALESCE(
        remark,
        '内地个人留学相关经常项目购汇：年度便利化额度、超额凭交易证明、近亲属可代办；'
        '学费宜直付学校收款账户。不含实时汇率或银行手续费报价。')
WHERE country = 'CN'
  AND policy_type = 'study_abroad'
  AND purpose_code IN ('tuition', 'living');

-- Document vocabulary used by Agent2 FX FAQ answers.
INSERT INTO DOCUMENT (doc_id, code, name_zh) VALUES
    ('doc-fx-tuition-fee-proof', 'tuition_fee_proof', '学费缴费通知书/学费证明'),
    ('doc-fx-passport-visa', 'passport_with_visa', '护照及有效签证/签注'),
    ('doc-fx-enrolment-proof', 'enrolment_proof', '在读证明/学生证'),
    ('doc-fx-living-cost-proof', 'living_cost_proof', '生活费相关费用证明'),
    ('doc-fx-purchase-app', 'fx_purchase_application', '购汇申请书'),
    ('doc-fx-proxy-id', 'proxy_id_document', '代办人有效身份证件'),
    ('doc-fx-beneficiary-checklist', 'remittance_beneficiary_checklist', '学校/收款方汇款信息核对清单')
ON CONFLICT (doc_id) DO NOTHING;
