"""Complete section extraction for the formal visa candidate pipeline."""
import json

from . import config, extract as legacy_extract
from .incremental import fingerprint
from .models import VisaDocument, VisaPatch, VisaPolicyData, VisaPolicyEntry, VisaEvidence


SECTION_PROMPT = legacy_extract.VISA_PROMPT + """
This input is ONE SECTION of a larger page. Return empty policies/documents when this
section contains no explicit policy facts. A missing item in this section does not
mean the full page removed it. For document requirements beginning with if/unless,
set condition to a concrete object (for example {"has_student_loan": true}); do
not mark a conditional document unconditional. Never convert a requirement to
funds_tuition_unpaid: that field is an AMOUNT. Use funds_tuition_required instead.
"""
SECTION_EXTRACTOR_VERSION = "sections-1"


def extraction_profile(settings, cache_epoch="initial"):
    body = {"model": settings["model"], "base_url": settings["base_url"],
            "extra_body": settings.get("extra_body"), "schema": config.SCHEMA_VERSION,
            "prompt": config.PROMPT_VERSIONS["visa_policy"], "section_prompt": SECTION_PROMPT,
            "chunk_limit": 3500, "extractor": SECTION_EXTRACTOR_VERSION,
            "cache_epoch": cache_epoch}
    return fingerprint(json.dumps(body, sort_keys=True, ensure_ascii=False))


def extract_section(section, source, client=None, settings=None):
    if client is None:
        client, settings = legacy_extract._client()
    response = legacy_extract._call(
        client, settings, SECTION_PROMPT,
        f"Source: {source['url']}\nSection: {section.heading}\n\n{section.text}",
        max_tokens=4000,
    )
    content = response.choices[0].message.content or ""
    if not content.strip():
        raise legacy_extract.ExtractionError("extraction_invalid", "章节抽取返回空 JSON")
    try:
        patch = VisaPatch.model_validate_json(content)
    except Exception as exc:
        raise legacy_extract.ExtractionError("extraction_invalid", f"章节抽取结构错误：{exc}") from exc
    return patch, legacy_extract._usage(response)


def merge_sections(section_patches):
    """Merge scoped facts; conflicting values are failures, never last-write-wins."""
    scoped = {}
    documents = {}
    for patch in section_patches:
        for entry in patch.policies:
            data = entry.policy.model_dump(exclude_none=True)
            scope = (data.get("applies_to_city") or "*").strip().casefold()
            bucket = scoped.setdefault(scope, {"fields": {}, "evidence": {}})
            for field, value in data.items():
                if field == "remark":
                    prior = bucket["fields"].get(field)
                    bucket["fields"][field] = f"{prior}\n{value}" if prior and value not in prior else value
                    continue
                if field == "conditions":
                    existing = bucket["fields"].setdefault(field, [])
                    for condition in value:
                        if condition not in existing:
                            existing.append(condition)
                    continue
                if field in bucket["fields"] and bucket["fields"][field] != value:
                    raise legacy_extract.ExtractionError(
                        "extraction_invalid", f"同一适用范围的 {field} 在章节间取值冲突")
                bucket["fields"][field] = value
            for ev in entry.evidence:
                bucket["evidence"].setdefault(ev.field, {})[ev.evidence_quote] = ev
        for document in patch.documents:
            key = document.code.strip().casefold()
            if key in documents:
                prior = documents[key]
                if prior.required != document.required or prior.condition != document.condition:
                    raise legacy_extract.ExtractionError(
                        "extraction_invalid", f"材料 {key} 在章节间必需性或条件冲突")
            else:
                documents[key] = document
    universal = scoped.pop("*", None)
    if universal and scoped:
        for bucket in scoped.values():
            for field, value in universal["fields"].items():
                if field == "applies_to_city":
                    continue
                if field == "remark":
                    prior = bucket["fields"].get(field)
                    bucket["fields"][field] = f"{prior}\n{value}" if prior and value not in prior else value
                    if field in universal["evidence"]:
                        bucket["evidence"].setdefault(field, {}).update(universal["evidence"][field])
                    continue
                if field == "conditions":
                    existing = bucket["fields"].setdefault(field, [])
                    for condition in value:
                        if condition not in existing:
                            existing.append(condition)
                    if field in universal["evidence"]:
                        bucket["evidence"].setdefault(field, {}).update(universal["evidence"][field])
                    continue
                if field in bucket["fields"] and bucket["fields"][field] != value:
                    raise legacy_extract.ExtractionError("extraction_invalid", f"通用规则 {field} 与区域规则冲突")
                bucket["fields"].setdefault(field, value)
                if field in universal["evidence"]:
                    bucket["evidence"].setdefault(field, {}).update(universal["evidence"][field])
    elif universal:
        scoped["*"] = universal
    entries = []
    for bucket in scoped.values():
        if not bucket["fields"]:
            continue
        entries.append(VisaPolicyEntry(
            policy=VisaPolicyData.model_validate(bucket["fields"]),
            evidence=[ev for values in bucket["evidence"].values() for ev in values.values()],
        ))
    if not entries and not documents:
        raise legacy_extract.ExtractionError("extraction_invalid", "整页没有抽取到签证要求")
    return VisaPatch(policies=entries, documents=list(documents.values()))
