"""Read Agent1 FX_POLICY / FX_EVIDENCE from PostgreSQL RDS."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from agent1 import config as agent1_config
from agent1.storage import read_session

from .models import Citation, Material
from .retrieve import display_url

PURPOSE_LABEL = {"tuition": "学费", "living": "生活费"}
REMITTER_LABEL = {
    "immediate_family": "直系亲属（本人或父母等）",
    "self_only": "仅限本人",
    "self": "本人",
    "family": "亲属",
    "close_relatives": "近亲属",
    "anyone_authorized": "经授权的代办人",
}
DOC_EN = {
    "id_document": "Valid identity document",
    "admission_letter": "Offer / admission letter",
    "tuition_fee_proof": "Tuition fee invoice or payment notice",
    "enrolment_proof": "Proof of enrolment",
    "passport_with_visa": "Passport with valid visa",
    "living_cost_proof": "Living-cost / expense proof",
    "kinship_proof": "Proof of kinship",
    "fx_purchase_application": "Foreign-exchange purchase application",
    "authorization_letter": "Authorization letter",
    "proxy_id_document": "Proxy identity document",
    "remittance_beneficiary_checklist": "Beneficiary / remittance field checklist",
    "other_document": "Supporting document",
}
FIELD_TARGETS = (
    "field:annual_quota_amount",
    "field:annual_quota_currency",
    "field:over_quota_allowed",
    "field:remitter_relation",
    "field:quota_reset",
    "field:can_use_family_quota",
    "field:tuition_direct_to_school",
    "field:remark",
)
SAFE_SOURCE_PREFIX = "safe_"


@dataclass(frozen=True)
class FxEvidenceRow:
    evidence_id: str
    target: str
    kind: str
    quote: str
    source_id: str
    recorded_at: str | None = None


@dataclass(frozen=True)
class FxDocumentRow:
    doc_id: str
    code: str
    name_zh: str
    required: bool
    condition: dict | None
    evidence: list[FxEvidenceRow] = field(default_factory=list)


@dataclass
class FxPolicyBundle:
    policy_id: str
    country: str
    purpose_code: str
    title: str
    annual_quota_amount: float | int | None
    annual_quota_currency: str | None
    over_quota_allowed: bool | None
    remitter_relation: str | None
    quota_reset: str | None
    can_use_family_quota: bool | None
    tuition_direct_to_school: bool | None = None
    remark: str | None = None
    evidence: list[FxEvidenceRow] = field(default_factory=list)
    documents: list[FxDocumentRow] = field(default_factory=list)
    sources: dict[str, dict[str, Any]] = field(default_factory=dict)
    verified_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    environment: str = "development"

    @property
    def purpose_zh(self) -> str:
        return PURPOSE_LABEL.get(self.purpose_code, self.purpose_code)


def _parse_condition(raw: Any) -> dict | None:
    if raw is None or raw == "":
        return None
    if isinstance(raw, dict):
        return raw
    try:
        parsed = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _condition_text(condition: dict | None) -> str | None:
    if not condition:
        return None
    groups = condition["any_of"] if set(condition) == {"any_of"} else [condition]
    parts = []
    for group in groups:
        bits = []
        for key, value in group.items():
            if key == "over_quota":
                bits.append("超过年度便利化额度" if value else "在年度便利化额度内")
            elif key == "by_proxy":
                bits.append("由他人代办" if value else "本人办理")
            elif key == "visa_deposit":
                bits.append("涉及签证保证金/生活费证明" if value else None)
            elif key == "study_year":
                bits.append({"first": "入学首年", "subsequent": "后续学年"}.get(
                    str(value), f"学年={value}"))
            else:
                bits.append(f"{key}={value}")
        bits = [b for b in bits if b]
        if bits:
            parts.append("且".join(bits))
    return "，或".join(parts) or None


def load_source_catalog(path: Path | None = None) -> dict[str, dict[str, Any]]:
    path = path or agent1_config.SOURCES_PATH
    rows = json.loads(Path(path).read_text(encoding="utf-8"))
    catalog = {}
    for row in rows:
        sid = row.get("id") or row.get("source_id")
        if not sid:
            continue
        catalog[sid] = {
            "source_id": sid,
            "name": row.get("name") or sid,
            "url": row.get("url") or "",
            "authority": row.get("authority") or "third_party",
        }
    return catalog


def _prefer_quote(rows: list[FxEvidenceRow]) -> FxEvidenceRow | None:
    if not rows:
        return None
    for row in rows:
        if row.source_id.startswith(SAFE_SOURCE_PREFIX) and row.quote.strip():
            return row
    for row in rows:
        if row.quote.strip():
            return row
    return None


class Agent1FxPolicyRead:
    """Load one study-abroad FX policy row plus evidence and documents."""

    def __init__(self, *, environment: str | None = None, sources_path: Path | None = None):
        self.environment = environment or agent1_config.ENVIRONMENT
        self.sources = load_source_catalog(sources_path)

    def get(self, *, purpose_code: str, remitter_country: str = "CN") -> FxPolicyBundle | None:
        if purpose_code not in PURPOSE_LABEL:
            return None
        with read_session() as con:
            policy = con.execute(
                """SELECT * FROM FX_POLICY
                   WHERE country=%s AND purpose_code=%s AND policy_type='study_abroad'
                   LIMIT 1""",
                (remitter_country, purpose_code),
            ).fetchone()
            if policy is None:
                return None
            evidence_rows = [
                FxEvidenceRow(
                    evidence_id=row["evidence_id"],
                    target=row["target"],
                    kind=row["kind"],
                    quote=(row["quote"] or "").strip(),
                    source_id=row["source_id"],
                    recorded_at=row["recorded_at"],
                )
                for row in con.execute(
                    "SELECT * FROM FX_EVIDENCE WHERE policy_id=%s ORDER BY recorded_at DESC",
                    (policy["policy_id"],),
                )
                if (row["quote"] or "").strip()
            ]
            by_doc: dict[str, list[FxEvidenceRow]] = {}
            for row in evidence_rows:
                if row.target.startswith("document:"):
                    by_doc.setdefault(row.target.removeprefix("document:"), []).append(row)
            documents = []
            for row in con.execute(
                """SELECT d.doc_id, d.code, d.name_zh, pd.required, pd."condition" AS condition
                   FROM POLICY_DOCUMENT pd
                   JOIN DOCUMENT d ON d.doc_id = pd.doc_id
                   WHERE pd.policy_id=%s AND pd.policy_domain='fx_policy'
                   ORDER BY d.name_zh""",
                (policy["policy_id"],),
            ):
                documents.append(FxDocumentRow(
                    doc_id=row["doc_id"],
                    code=row["code"],
                    name_zh=row["name_zh"],
                    required=bool(row["required"]),
                    condition=_parse_condition(row["condition"]),
                    evidence=by_doc.get(row["doc_id"], []),
                ))
        stamps = [r.recorded_at for r in evidence_rows if r.recorded_at]
        verified_at = datetime.fromisoformat(max(stamps).replace("Z", "+00:00")) if stamps \
            else datetime.now(timezone.utc)
        if verified_at.tzinfo is None:
            verified_at = verified_at.replace(tzinfo=timezone.utc)
        return FxPolicyBundle(
            policy_id=policy["policy_id"],
            country=policy["country"],
            purpose_code=policy["purpose_code"],
            title=policy["title"],
            annual_quota_amount=policy["annual_quota_amount"],
            annual_quota_currency=policy["annual_quota_currency"],
            over_quota_allowed=None if policy["over_quota_allowed"] is None
            else bool(policy["over_quota_allowed"]),
            remitter_relation=policy["remitter_relation"],
            quota_reset=policy["quota_reset"],
            can_use_family_quota=None if policy["can_use_family_quota"] is None
            else bool(policy["can_use_family_quota"]),
            tuition_direct_to_school=None if policy.get("tuition_direct_to_school") is None
            else bool(policy["tuition_direct_to_school"]),
            remark=policy.get("remark"),
            evidence=evidence_rows,
            documents=documents,
            sources=self.sources,
            verified_at=verified_at,
            environment=self.environment,
        )


def build_fx_answer_parts(bundle: FxPolicyBundle, *, destination_label: str,
                          mode: str) -> tuple[list[Citation], list[Material], list[str], dict]:
    """Deterministic citations, materials, quote snippets and verified facts for the LLM.

    One citation per unique source (URL / name); materials point at that source's id.
    """
    citations: list[Citation] = []
    materials: list[Material] = []
    quotes: list[str] = []
    by_source: dict[str, str] = {}  # source key -> citation_id

    def _source_key(row: FxEvidenceRow) -> str:
        meta = bundle.sources.get(row.source_id) or {}
        url = display_url(meta.get("url") or "") or ""
        return url or meta.get("name") or row.source_id

    def ensure_citation(row: FxEvidenceRow) -> str:
        key = _source_key(row)
        existing = by_source.get(key)
        if existing:
            return existing
        cid = f"C{len(citations) + 1}"
        meta = bundle.sources.get(row.source_id) or {}
        name = meta.get("name") or row.source_id
        citations.append(Citation(
            citation_id=cid,
            record_id=bundle.policy_id,
            revision_id=f"fx-working:{bundle.environment}",
            evidence_id=row.evidence_id,
            source_name=name,
            source_url=display_url(meta.get("url") or ""),
            quote=row.quote,
            verified_at=bundle.verified_at,
            synthetic=False,
        ))
        by_source[key] = cid
        quotes.append(f"[{cid}] {name}：{row.quote}")
        return cid

    used_evidence: set[str] = set()
    used_quotes: set[str] = set()
    for target in FIELD_TARGETS:
        row = _prefer_quote([e for e in bundle.evidence if e.target == target and e.kind == "value"])
        if not row or row.evidence_id in used_evidence:
            continue
        if row.quote in used_quotes:
            used_evidence.add(row.evidence_id)
            continue
        used_evidence.add(row.evidence_id)
        used_quotes.add(row.quote)
        ensure_citation(row)

    # Prefer identity / fee proofs; skip proxy-only paperwork unless no other docs.
    ranked_docs = sorted(
        [d for d in bundle.documents if d.required],
        key=lambda d: (
            0 if not d.condition else
            1 if d.condition and "by_proxy" not in d.condition else 2,
            d.name_zh,
        ),
    )
    for doc in ranked_docs:
        row = _prefer_quote([e for e in doc.evidence if e.kind == "value"] or doc.evidence)
        if row is None:
            cid = next(iter(by_source.values()), None)
            if cid is None:
                continue
        else:
            cid = ensure_citation(row)
            used_evidence.add(row.evidence_id)
            used_quotes.add(row.quote)
        cond_text = _condition_text(doc.condition)
        applicability = "required" if not doc.condition else "conditional"
        materials.append(Material(
            requirement_key=f"{doc.code}:{doc.doc_id[:8]}",
            document_code=doc.code,
            name_zh=doc.name_zh,
            name_en=DOC_EN.get(doc.code, ""),
            applicability=applicability,
            condition_text=cond_text,
            citation_id=cid,
        ))

    remitter = REMITTER_LABEL.get(bundle.remitter_relation or "", bundle.remitter_relation)
    facts = {
        "policy_id": bundle.policy_id,
        "policy_title": bundle.title,
        "remitter_country": bundle.country,
        "destination": destination_label,
        "purpose_code": bundle.purpose_code,
        "purpose_zh": bundle.purpose_zh,
        "mode": mode,
        "annual_quota_amount": bundle.annual_quota_amount,
        "annual_quota_currency": bundle.annual_quota_currency,
        "over_quota_allowed": bundle.over_quota_allowed,
        "remitter_relation": remitter,
        "remitter_relation_code": bundle.remitter_relation,
        "quota_reset": bundle.quota_reset,
        "can_use_family_quota": bundle.can_use_family_quota,
        "tuition_direct_to_school": bundle.tuition_direct_to_school,
        "remark": bundle.remark,
        "quotes": quotes,
        "materials": [
            {"name_zh": m.name_zh, "applicability": m.applicability,
             "condition_text": m.condition_text, "citation_id": m.citation_id,
             "document_code": m.document_code}
            for m in materials
        ],
    }
    return citations, materials, quotes, facts


FxPolicyLoader = Callable[..., FxPolicyBundle | None]
