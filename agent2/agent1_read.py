"""M07：读取 Agent1 已发布的签证政策（agent1.publication.search / get）。

只读 Agent1 发布面，不连接 Agent1 工作表。Agent1 的条件是抽取时生成的键值（如 under_18），
这里只把认得的键映射到 Profile 字段；任一键映射不了，整条材料记为适用条件未知（待核实），
绝不当作无条件需要。
"""
from collections import Counter

from agent1 import config as agent1_config
from agent1 import publication
from agent1.environment import paths_for

from .models import Condition, KnowledgeRecord, ReadRecord, Requirement, Source

POLICY_TYPES = {"student_visa": "student"}
TOPICS = {"documents"}

EN_NAMES = {
    "passport": "Passport or other valid travel document",
    "cas": "Confirmation of Acceptance for Studies (CAS)",
    "proof_of_funds": "Proof of funds",
    "parental_consent": "Parental or legal guardian consent",
    "birth_certificate": "Proof of relationship to parent or guardian",
    "tuberculosis_test_certificate": "Tuberculosis (TB) test results",
    "student_loan_evidence": "Student loan evidence",
    "financial_sponsorship_evidence": "Financial sponsor's written consent",
    "atas": "Academic Technology Approval Scheme (ATAS) certificate",
    "english_test_result": "Proof of knowledge of English",
    "form_i20": "Form I-20, Certificate of Eligibility for Nonimmigrant Student Status",
    "sevis_fee_receipt": "I-901 SEVIS fee payment receipt",
    "photo": "Photograph",
    "admission_letter": "Letter of acceptance",
}
COUNTRY_EN_NAMES = {
    ("US", "application_form"): "Form DS-160 confirmation page",
    ("CN-HK", "application_form"): "Application for Entry for Study in Hong Kong (ID 995A)",
    ("CN-HK", "proof_of_funds"): "Proof of the applicant's financial support",
    ("CN-HK", "financial_sponsorship_evidence"): "Proof of the sponsor's financial standing and undertaking",
    ("CN-HK", "parental_consent"): "Consent letter signed by the guardian and one parent",
}


def _when_true(field, op, text, value=None):
    def build(raw):
        if raw is not True:
            return None
        return [Condition(field=field, op=op, value=value, text=text)]
    return build


def _age_16_or_17(raw):
    if raw is True or raw == "16 or 17":
        return [Condition(field="applicant_age", op="in", value=[16, 17], text="申请人 16 或 17 岁")]
    return None


_UNDER_18 = _when_true("applicant_age", "lt", "申请人未满 18 岁", 18)
_STAY = _when_true("stay_over_6_months", "is_true", "在英国停留 6 个月或以上")
_TB_RESIDENT = _when_true("tb_listed_country_resident", "is_true",
                          "在需做肺结核检测的国家或地区住满 6 个月且最近 6 个月内仍住在那里")

def _by_value(field, choices):
    """choices: Agent1 value -> (op, Profile value, text)."""
    def build(raw):
        if not isinstance(raw, (bool, str)) or raw not in choices:
            return None
        op, value, text = choices[raw]
        return [Condition(field=field, op=op, value=value, text=text)]
    return build


_HK_SPONSOR = _by_value("hk_sponsor", {
    True: ("eq", "institution", "香港本地保证人是录取院校"),
    False: ("eq", "individual", "香港本地保证人是个人")})
_HK_SPONSOR_PR = _by_value("hk_sponsor_permanent_resident", {
    True: ("is_true", None, "保证人是香港永久性居民"),
    False: ("is_false", None, "保证人不是香港永久性居民")})
_RESIDENCE = _by_value("applicant_residence", {
    "Macao SAR": ("eq", "MO", "申请人是澳门居民"),
    "Taiwan": ("eq", "TW", "申请人是台湾居民"),
    "Mainland": ("eq", "CN", "申请人是中国内地居民"),
    "Mainland China": ("eq", "CN", "申请人是中国内地居民")})

CONDITION_MAP = {
    "under_18": _UNDER_18,
    "applicant_under_18": _UNDER_18,
    "sponsor_is_educational_institution": _HK_SPONSOR,
    "sponsor_is_hk_permanent_resident": _HK_SPONSOR_PR,
    "applicant_residence": _RESIDENCE,
    "holds_hk_identity_card": _when_true("has_hk_identity_card", "is_true", "申请人持有香港身份证"),
    "age": _age_16_or_17,
    "age_16_or_17": _age_16_or_17,
    "has_student_loan": _when_true("funding_source", "eq", "资金来自学生贷款", "student_loan"),
    "has_financial_sponsorship": _when_true("funding_source", "eq", "由机构资助（financial sponsor）", "sponsor"),
    "coming_to_uk_for_6_months_or_more": _STAY,
    "coming_for_6_months_or_more": _STAY,
    "stay_6_months_or_more": _STAY,
    "lived_in_listed_country_for_6_months_or_more": _TB_RESIDENT,
    "residence_in_listed_country_6_months_or_more": _TB_RESIDENT,
    "living_there_within_last_6_months": _TB_RESIDENT,
    "living_in_listed_country_within_last_6_months": _TB_RESIDENT,
}


def _key(condition):
    return condition.model_dump(exclude={"text"})


def map_condition(condition):
    """Agent1 condition -> OR-list of AND-groups of Profile conditions, or None if any key is unknown."""
    groups = condition["any_of"] if set(condition) == {"any_of"} else [condition]
    mapped = []
    for group in groups:
        conditions = []
        for name, raw in group.items():
            build = CONDITION_MAP.get(name)
            result = build(raw) if build else None
            if not result:
                return None
            conditions += [c for c in result if all(_key(c) != _key(x) for x in conditions)]
        if all([_key(c) for c in conditions] != [_key(c) for c in other] for other in mapped):
            mapped.append(conditions)
    return mapped


def _source(source_id, sources):
    source = sources.get(source_id) or {}
    return Source(source_id=source_id, name=source.get("name") or source_id, url=source.get("url") or "",
                  authority="official" if source.get("authority") == "official" else "third_party")


def _requirement(document, sources, country):
    if document["required"] is False and not document["condition"]:
        return None
    values = [e for e in document["evidence"] if e["kind"] == "value"]
    conditions = [e for e in document["evidence"] if e["kind"] == "condition"]
    if not values:
        return None
    condition_sources = {e["source_id"] for e in conditions}
    evidence = next((e for e in values if e["source_id"] in condition_sources), values[0])
    code, short = document["code"], document["doc_id"][:8]
    fields = dict(
        requirement_key=f"{code}:{short}",
        document_code=f"other_document:{short}" if code == "other_document" else code,
        name_zh=document["name_zh"], required=True,
        name_en=COUNTRY_EN_NAMES.get((country, code)) or EN_NAMES.get(code, ""),
        evidence_id=evidence["evidence_id"], evidence_quote=evidence["quote"],
        condition_quote=" | ".join(e["quote"] for e in conditions) or None,
        source=_source(evidence["source_id"], sources),
    )
    groups = map_condition(document["condition"]) if document["required"] and document["condition"] else None
    if document["required"] and not document["condition"]:
        return Requirement(**fields, applicability_status="unconditional")
    if groups is None:
        return Requirement(**fields, applicability_status="unknown")
    if len(groups) == 1:
        return Requirement(**fields, applicability_status="conditional", condition=groups[0])
    return Requirement(**fields, applicability_status="conditional", any_of=groups)


def to_read_record(envelope, visa_category="student_visa", topic="documents"):
    sources = envelope["sources"]
    counts = Counter(e["source_id"] for e in envelope["evidence"] if e["target"].startswith("document:"))
    primary = counts.most_common(1)[0][0] if counts else next(iter(sources), "")
    country = envelope["scope"]["country"]
    requirements = [r for d in envelope["payload"]["documents"] if (r := _requirement(d, sources, country))]
    record = KnowledgeRecord(
        record_id=envelope["record_id"], revision_id=envelope["revision_id"],
        schema_version=envelope["schema_version"], country=envelope["scope"]["country"],
        visa_category=visa_category, topic=topic,
        status="WITHDRAWN" if envelope["availability"] == "withdrawn" else "ACTIVE",
        synthetic=False, verified_at=envelope["verified_at"],
        effective_from=envelope["effective_from"], effective_to=envelope["effective_to"],
        source=_source(primary, sources), snapshot=None, requirements=requirements,
    )
    return ReadRecord(record=record, available=envelope["availability"] == "available",
                      availability_reason=envelope["availability"])


class Agent1KnowledgeRead:
    def __init__(self, paths=None):
        self.paths = paths or agent1_config.PATHS
        self.read_id = f"agent1:{self.paths.environment}"

    @classmethod
    def for_environment(cls, environment):
        return cls(paths_for(agent1_config.ROOT, environment))

    def search(self, *, country, visa_category, topic, now):
        policy_type = POLICY_TYPES.get(visa_category)
        if policy_type is None or topic not in TOPICS:
            return []
        envelopes = publication.search(country, policy_type, now=now, paths=self.paths)
        return [to_read_record(e, visa_category, topic) for e in envelopes]

    def get(self, record_id, revision_id, *, now):
        envelope = publication.get(record_id, revision_id, now=now, paths=self.paths)
        return to_read_record(envelope) if envelope else None

    def snapshot(self, revision_id):
        envelope = publication.get(None, revision_id, paths=self.paths)
        if envelope is None:
            return None
        return {
            "record_id": envelope["record_id"], "revision_id": envelope["revision_id"],
            "synthetic": False, "verified_at": envelope["verified_at"],
            "availability": envelope["availability"], "sources": envelope["sources"],
            "evidence": [{k: e.get(k) for k in ("evidence_id", "target", "kind", "quote",
                                                "source_name", "source_url")}
                         for e in envelope["evidence"]],
        }
