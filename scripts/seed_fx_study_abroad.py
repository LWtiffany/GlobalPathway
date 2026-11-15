#!/usr/bin/env python3
"""Seed curated CN study-abroad FX policy fields, materials, and evidence."""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agent1 import storage  # noqa: E402

MIGRATION = ROOT / "agent1" / "migrations" / "024_fx_study_abroad_seed.sql"
NOW = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
SNAPSHOT = "seed-fx-study-abroad-024"

DOCS = [
    ("doc-fx-tuition-fee-proof", "tuition_fee_proof", "学费缴费通知书/学费证明"),
    ("doc-fx-passport-visa", "passport_with_visa", "护照及有效签证/签注"),
    ("doc-fx-enrolment-proof", "enrolment_proof", "在读证明/学生证"),
    ("doc-fx-living-cost-proof", "living_cost_proof", "生活费相关费用证明"),
    ("doc-fx-purchase-app", "fx_purchase_application", "购汇申请书"),
    ("doc-fx-proxy-id", "proxy_id_document", "代办人有效身份证件"),
    ("doc-fx-beneficiary-checklist", "remittance_beneficiary_checklist",
     "学校/收款方汇款信息核对清单"),
]

# (purpose, code, required, condition_json)
POLICY_DOCS = [
    ("tuition", "admission_letter", 1, None),
    ("tuition", "tuition_fee_proof", 1, None),
    ("tuition", "passport_with_visa", 1, '{"over_quota": true}'),
    ("tuition", "enrolment_proof", 1, '{"over_quota": true}'),
    ("tuition", "fx_purchase_application", 0, None),
    ("tuition", "remittance_beneficiary_checklist", 1, None),
    ("tuition", "proxy_id_document", 1, '{"by_proxy": true}'),
    ("living", "living_cost_proof", 1, '{"over_quota": true}'),
    ("living", "passport_with_visa", 1, '{"over_quota": true}'),
    ("living", "enrolment_proof", 1, '{"over_quota": true}'),
    ("living", "fx_purchase_application", 0, None),
    ("living", "remittance_beneficiary_checklist", 0, None),
    ("living", "proxy_id_document", 1, '{"by_proxy": true}'),
]

# Shared field / remark quotes (SAFE / bank pages already in sources.json).
FIELD_EVIDENCE = [
    ("field:annual_quota_amount", "value", "safe_personal_fx_rules",
     "年度总额分别为每人每年等值5万美元。"),
    ("field:annual_quota_currency", "value", "safe_personal_fx_rules",
     "年度总额分别为每人每年等值5万美元。"),
    ("field:quota_reset", "value", "safe_personal_fx_rules",
     "年度总额分别为每人每年等值5万美元。"),
    ("field:over_quota_allowed", "value", "safe_personal_fx_rules",
     "境内个人经常项目项下非经营性购汇超过年度总额的，凭本人有效身份证件和有交易额的相关证明材料在银行办理。"),
    ("field:remitter_relation", "value", "safe_tianjin_proxy_fx",
     "近亲属代为办理的，应分别提供委托人和受托人的有效身份证件、委托人的授权书、直系亲属关系证明。"),
    ("field:can_use_family_quota", "value", "safe_tianjin_study_fx",
     "个人购汇实行年度总额管理；留学等经常项目项下可按规定凭有效证件及相关证明材料办理。"),
    ("field:tuition_direct_to_school", "value", "hsbc_cn_education_payment",
     "教育类跨境支付宜按学校缴费通知将学费汇至学校指定收款账户，并保留付款凭证。"),
]

REMARK_EVIDENCE = [
    ("field:remark", "value", "safe_tianjin_study_fx",
     "留学购汇应提供与学费或生活费相关的证明材料；具体材料以办理银行审核要求为准。"),
    ("field:remark", "value", "boc_outbound_remittance",
     "汇出境外汇款需准确填写收款人名称、账号、开户银行及SWIFT等汇款信息。"),
    ("field:remark", "value", "icbc_personal_fx_purchase",
     "境外消费与购汇额度管理相关；刷卡清算规则以发卡行及支付网络为准，银行不提供实时成交牌价作为本服务承诺。"),
]

DOC_EVIDENCE = [
    ("tuition_fee_proof", "safe_tianjin_study_fx",
     "留学购汇需提供学费相关证明（如学校缴费通知书/学费账单）。"),
    ("admission_letter", "safe_tianjin_study_fx",
     "留学购汇可提供录取通知书等与留学相关的证明材料。"),
    ("passport_with_visa", "safe_tianjin_study_fx",
     "办理留学相关购汇时，银行可能要求核验护照及签证/签注等身份与入境文件。"),
    ("living_cost_proof", "safe_tianjin_study_fx",
     "生活费购汇应提供与生活费用相关的证明材料，具体以银行审核为准。"),
    ("enrolment_proof", "icbc_personal_fx_purchase",
     "后续学年或在读期间办理时，银行可能要求提供在读证明等材料。"),
    ("remittance_beneficiary_checklist", "boc_outbound_remittance",
     "汇款前请核对收款人名称、账号、开户银行名称、SWIFT/BIC、币种、金额及学生姓名/学号等备注信息。"),
    ("proxy_id_document", "safe_tianjin_proxy_fx",
     "近亲属代办购汇应提供受托人有效身份证件。"),
    ("authorization_letter", "safe_tianjin_proxy_fx",
     "代办购汇应提供委托人的授权书。"),
    ("kinship_proof", "safe_tianjin_proxy_fx",
     "近亲属代办应提供直系亲属关系证明。"),
]


def _run_sql_file(con, path: Path) -> None:
    raw = path.read_text(encoding="utf-8")
    lines = []
    for ln in raw.splitlines():
        stripped = ln.strip()
        if stripped.startswith("--") or not stripped:
            continue
        lines.append(ln)
    sql = "\n".join(lines)
    for chunk in sql.split(";"):
        cleaned = chunk.strip()
        if cleaned:
            con.execute(cleaned)


def _code_to_doc_id(con) -> dict[str, str]:
    mapping = {}
    for row in con.execute("SELECT doc_id, code FROM DOCUMENT"):
        mapping.setdefault(row["code"], row["doc_id"])
    for doc_id, code, _ in DOCS:
        mapping[code] = doc_id
    return mapping


def _upsert_evidence(con, policy_id: str, target: str, kind: str,
                     source_id: str, quote: str) -> None:
    eid = f"seed:{policy_id[:8]}:{target}:{source_id}"
    con.execute(
        """INSERT INTO FX_EVIDENCE
               (evidence_id, policy_id, target, kind, source_id, snapshot_id, quote, recorded_at)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
           ON CONFLICT (evidence_id) DO UPDATE SET
               quote = EXCLUDED.quote,
               recorded_at = EXCLUDED.recorded_at,
               snapshot_id = EXCLUDED.snapshot_id""",
        (eid, policy_id, target, kind, source_id, SNAPSHOT, quote, NOW),
    )


def main() -> int:
    with storage.session(readonly=False) as con:
        _run_sql_file(con, MIGRATION)
        for doc_id, code, name_zh in DOCS:
            con.execute(
                """INSERT INTO DOCUMENT (doc_id, code, name_zh)
                   VALUES (%s, %s, %s)
                   ON CONFLICT (doc_id) DO UPDATE SET name_zh = EXCLUDED.name_zh""",
                (doc_id, code, name_zh),
            )
        code_ids = _code_to_doc_id(con)
        policies = {
            row["purpose_code"]: row["policy_id"]
            for row in con.execute(
                """SELECT policy_id, purpose_code FROM FX_POLICY
                   WHERE country='CN' AND policy_type='study_abroad'
                     AND purpose_code IN ('tuition','living')""")
        }
        if set(policies) != {"tuition", "living"}:
            raise SystemExit(f"expected tuition+living CN policies, got {policies!r}")

        for purpose, code, required, cond in POLICY_DOCS:
            doc_id = code_ids.get(code)
            if not doc_id:
                # admission_letter / kinship etc. may pre-exist under other ids
                row = con.execute(
                    "SELECT doc_id FROM DOCUMENT WHERE code=%s LIMIT 1", (code,)
                ).fetchone()
                if not row:
                    print(f"skip missing document code={code}")
                    continue
                doc_id = row["doc_id"]
            con.execute(
                """INSERT INTO POLICY_DOCUMENT
                       (policy_id, policy_domain, doc_id, required, "condition")
                   VALUES (%s, 'fx_policy', %s, %s, %s)
                   ON CONFLICT (policy_id, policy_domain, doc_id) DO UPDATE SET
                       required = EXCLUDED.required,
                       "condition" = COALESCE(EXCLUDED."condition", POLICY_DOCUMENT."condition")""",
                (policies[purpose], doc_id, required, cond),
            )

        for purpose, policy_id in policies.items():
            for target, kind, source_id, quote in FIELD_EVIDENCE:
                _upsert_evidence(con, policy_id, target, kind, source_id, quote)
            for target, kind, source_id, quote in REMARK_EVIDENCE:
                # Overseas-spend note mainly on living; tuition gets remittance checklist note.
                if source_id == "icbc_personal_fx_purchase" and purpose != "living":
                    continue
                _upsert_evidence(con, policy_id, target, kind, source_id, quote)
            for code, source_id, quote in DOC_EVIDENCE:
                doc_id = code_ids.get(code)
                if not doc_id:
                    row = con.execute(
                        "SELECT doc_id FROM DOCUMENT WHERE code=%s LIMIT 1", (code,)
                    ).fetchone()
                    if not row:
                        continue
                    doc_id = row["doc_id"]
                # Skip living-only docs on tuition and vice versa lightly.
                if purpose == "tuition" and code == "living_cost_proof":
                    continue
                if purpose == "living" and code in {"tuition_fee_proof", "admission_letter"}:
                    continue
                _upsert_evidence(
                    con, policy_id, f"document:{doc_id}", "value", source_id, quote)

        con.execute(
            """UPDATE FX_POLICY SET
                   remitter_relation = COALESCE(remitter_relation, 'immediate_family'),
                   can_use_family_quota = COALESCE(can_use_family_quota, 1),
                   tuition_direct_to_school = COALESCE(tuition_direct_to_school, 1)
               WHERE policy_id = ANY(%s)""",
            (list(policies.values()),),
        )
    print("seeded FX study-abroad policies:", ", ".join(
        f"{k}={v}" for k, v in policies.items()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
