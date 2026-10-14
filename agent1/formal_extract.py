"""Complete section extraction for the formal visa candidate pipeline."""
import json

from . import config, extract as legacy_extract, gate
from .incremental import fingerprint
from .models import DocumentSupport, VisaDocument, VisaPatch, VisaPolicyData, VisaPolicyEntry, VisaEvidence


SECTION_PROMPT = legacy_extract.VISA_PROMPT + """
This input is ONE SECTION of a larger page. Return empty policies/documents when this
section contains no explicit policy facts. A missing item in this section does not
mean the full page removed it. For document requirements beginning with if/unless,
set condition to a concrete object (for example {"has_student_loan": true}); do
not mark a conditional document unconditional. Never convert a requirement to
funds_tuition_unpaid: that field is an AMOUNT. Use funds_tuition_required instead.
Operator notes for this source, when present, narrow what to extract from it.
"""
SECTION_EXTRACTOR_VERSION = "sections-2"

# A scope naming the destination itself is not a narrower scope: it is the universal rule.
DESTINATION_SCOPES = {
    "UK": {"uk", "the uk", "united kingdom", "the united kingdom", "great britain", "england"},
    "US": {"us", "usa", "u.s.", "the us", "the u.s.", "united states", "the united states",
           "united states of america"},
    "CN-HK": {"hong kong", "hksar", "the hksar", "hong kong sar", "hong kong special administrative region"},
}

# Documents whose unconditional requirement is exactly a boolean policy column.
DOCUMENT_FLAGS = {"cas": "cas_required", "form_i20": "i20_required"}
REQUIREMENT_FLAGS = {"cas_required", "i20_required", "tb_test_required", "funds_tuition_required"}


def extraction_profile(settings, cache_epoch="initial", source_notes=None):
    body = {"model": settings["model"], "base_url": settings["base_url"],
            "extra_body": settings.get("extra_body"), "schema": config.SCHEMA_VERSION,
            "prompt": config.PROMPT_VERSIONS["visa_policy"], "section_prompt": SECTION_PROMPT,
            "chunk_limit": 3500, "extractor": SECTION_EXTRACTOR_VERSION,
            "cache_epoch": cache_epoch, "source_notes": source_notes}
    return fingerprint(json.dumps(body, sort_keys=True, ensure_ascii=False))


def _normalize_entries(content):
    """归一化 LLM 输出结构：条目偶尔被写扁成平铺字段（缺 policy 键）。
    凡缺少 policy 但含 Schema 字段的条目，按 {"policy": 条目, "evidence": []} 包回。"""
    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        return content
    if not isinstance(data, dict) or not isinstance(data.get("policies"), list):
        return content
    changed = False
    for index, entry in enumerate(data["policies"]):
        if not isinstance(entry, dict) or "policy" in entry:
            continue
        if set(entry) & set(VisaPolicyData.model_fields):
            evidence = entry.pop("evidence", None)
            data["policies"][index] = {"policy": entry, "evidence": evidence or []}
            changed = True
    return json.dumps(data, ensure_ascii=False) if changed else content


def extract_section(section, source, client=None, settings=None):
    if client is None:
        client, settings = legacy_extract._client()
    notes = source.get("extraction_notes")
    header = f"Source: {source['url']}\n"
    if notes:
        header += f"Operator notes: {notes}\n"
    response = legacy_extract._call(
        client, settings, SECTION_PROMPT,
        f"{header}Section: {section.heading}\n\n{section.text}",
        max_tokens=4000,
    )
    content = response.choices[0].message.content or ""
    if not content.strip():
        raise legacy_extract.ExtractionError("extraction_invalid", "章节抽取返回空 JSON")
    try:
        patch = VisaPatch.model_validate_json(_normalize_entries(content))
    except Exception as exc:
        raise legacy_extract.ExtractionError("extraction_invalid", f"章节抽取结构错误：{exc}") from exc
    return patch, legacy_extract._usage(response)


def _scope_key(city, region):
    scope = (city or "*").strip().casefold()
    if scope in DESTINATION_SCOPES.get(region, ()):
        return "*"
    return scope


def _conflict(conflicts, message, **details):
    if conflicts is None:
        raise legacy_extract.ExtractionError("extraction_invalid", message)
    conflicts.append({"reason": message, **details})


def _condition_alternatives(document):
    condition = document.condition or {}
    return list(condition["any_of"]) if set(condition) == {"any_of"} else [condition]


def _union_support(*documents):
    seen, support = set(), []
    for document in documents:
        for item in document.support:
            key = (item.source_id, item.snapshot_id, item.evidence_quote, item.condition_quote)
            if key not in seen:
                seen.add(key)
                support.append(item)
    return support


def _required_rank(required):
    """必需 > 条件未知 > 仅提及。"""
    return {True: 2, None: 1, False: 0}[required]


def _combine_documents(prior, document):
    """Same document, same required-ness, different conditions (lenient merge only).

    An unconditional requirement subsumes conditional ones; otherwise the document
    is required when ANY of the stated conditions holds. Each condition keeps its own
    verbatim quote, joined by gate.CONDITION_QUOTE_SEPARATOR.
    """
    if prior.condition == document.condition:
        return prior.model_copy(update={"support": _union_support(prior, document)})
    if not prior.condition or not document.condition:
        return prior if not prior.condition else document
    alternatives = _condition_alternatives(prior)
    alternatives += [c for c in _condition_alternatives(document) if c not in alternatives]
    quotes = []
    for doc in (prior, document):
        for part in (doc.condition_quote or "").split(gate.CONDITION_QUOTE_SEPARATOR):
            if part and part not in quotes:
                quotes.append(part)
    return prior.model_copy(update={
        "condition": {"any_of": alternatives},
        "condition_quote": gate.CONDITION_QUOTE_SEPARATOR.join(quotes) or None,
        "support": _union_support(prior, document),
    })


def mark_unstated_conditions(patch, markdown=None):
    """原文带 if/unless 但没抽出条件的“必需”材料改为条件未知，不能当作无条件必需。

    markdown 给出时，引用截掉了所在行方括号限定（如 “[if ...]”）的也算。
    """
    def unstated(d):
        quote = d.evidence_quote or ""
        return gate.CONDITION_WORDS.search(quote) or (
            markdown is not None and gate.omits_bracketed_condition(quote, markdown))

    documents = [
        d.model_copy(update={"required": None}) if d.required and not d.condition and unstated(d) else d
        for d in patch.documents]
    return patch.model_copy(update={"documents": documents})


def attribute(patch, source_id, snapshot_id):
    """给页面门控后的字段证据与材料打上来源与快照。"""
    if not (source_id and snapshot_id):
        return patch
    entries = [VisaPolicyEntry(
        policy=entry.policy,
        evidence=[ev if ev.source_id else ev.model_copy(update={"source_id": source_id,
                                                                "snapshot_id": snapshot_id})
                  for ev in entry.evidence])
        for entry in patch.policies]
    documents = [d if d.support else d.model_copy(update={"support": [DocumentSupport(
        source_id=source_id, snapshot_id=snapshot_id, evidence_quote=d.evidence_quote,
        condition_quote=d.condition_quote)]}) for d in patch.documents]
    return VisaPatch(policies=entries, documents=documents)


def merge_sections(section_patches, conflicts=None, region=None):
    """Merge scoped facts; conflicting values are failures, never last-write-wins.

    With ``conflicts`` (a list) the merge does not raise: the first value is kept,
    each conflict is appended as {"reason", "scope", "field", "values"} and the
    caller must route the field to human review. Documents then resolve a
    required/not-required disagreement to required (required=false only means the
    page mentions the item without demanding it).
    """
    scoped = {}
    documents = {}
    for patch in section_patches:
        for entry in patch.policies:
            data = entry.policy.model_dump(exclude_none=True)
            scope = _scope_key(data.get("applies_to_city"), region)
            if scope == "*":
                data.pop("applies_to_city", None)
            bucket = scoped.setdefault(scope, {"fields": {}, "evidence": {}, "conflicted": set()})
            rejected = set()
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
                if (conflicts is not None and field in REQUIREMENT_FLAGS
                        and {bucket["fields"].get(field), value} == {True, False}):
                    # 一处陈述要求、另一处因豁免写成 false：要求成立，豁免已在 conditions
                    if value is False:
                        rejected.add(field)
                        continue
                    bucket["evidence"].pop(field, None)
                elif field in bucket["fields"] and bucket["fields"][field] != value:
                    _conflict(conflicts, f"同一适用范围的 {field} 在章节间取值冲突",
                              scope=data.get("applies_to_city"), field=field,
                              values=[bucket["fields"][field], value])
                    bucket["conflicted"].add(field)
                    rejected.add(field)
                    continue
                bucket["fields"][field] = value
            for ev in entry.evidence:
                if ev.field in rejected or (ev.field == "applies_to_city" and scope == "*"):
                    continue
                bucket["evidence"].setdefault(ev.field, {})[ev.evidence_quote] = ev
        for document in patch.documents:
            # other_document 是兜底码：同一页面多种不同材料会共用它（如香港页的
            # 录取通知书/ID 995A/旅行证件）。标识必须是 (code, name_zh)，
            # 否则不同材料被误判为冲突；同码同名的分歧按下面分级处理。
            key = (document.code.strip().casefold(), (document.name_zh or "").strip().casefold())
            prior = documents.get(key)
            if prior is None:
                documents[key] = document
                continue
            if prior.required != document.required:
                if conflicts is None:
                    raise legacy_extract.ExtractionError(
                        "extraction_invalid",
                        f"材料 {key[0]}/{key[1]} 在章节间必需性冲突"
                        f"（{prior.required} vs {document.required}）")
                documents[key] = (prior if _required_rank(prior.required)
                                  >= _required_rank(document.required) else document)
                continue
            if conflicts is not None:
                documents[key] = _combine_documents(prior, document)
                continue
            if prior.condition == document.condition:
                continue  # 完全重复
            # 条件表述不同：若一条引用是另一条的子串，是同一要求的详略写法
            # （模型换了个 condition key 拼写），保留更具体的那条；
            # 否则视为真实分歧——按设计拒绝整页，不得静默取舍。
            shorter, longer = ((prior, document)
                               if len(prior.evidence_quote) <= len(document.evidence_quote)
                               else (document, prior))
            if shorter.evidence_quote and shorter.evidence_quote in longer.evidence_quote:
                documents[key] = longer
                continue
            _conflict(conflicts,
                      f"材料 {key[0]}/{key[1]} 在章节间条件冲突且无法判定详略"
                      f"（{prior.condition} vs {document.condition}）",
                      scope=None, field=f"document:{key[0]}",
                      values=[prior.condition, document.condition])
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
                    # 通用规则与范围规则不一致：范围特定的值更具体，优先保留；
                    # 被覆盖的通用值记入 remark 保持可追溯（remark 不参与 business_hash）。
                    # 典型场景：页面同时存在"例行申请 21 天"与"内地生申请 42 天"，
                    # 模型未给后者打范围标签时落入通用桶——不应因此整页失败。
                    note = f"通用规则 {field}={value} 被本范围取值 {bucket['fields'][field]} 覆盖"
                    prior = bucket["fields"].get("remark")
                    bucket["fields"]["remark"] = f"{prior}\n{note}" if prior else note
                    continue
                bucket["fields"].setdefault(field, value)
                if field in universal["conflicted"]:
                    bucket["conflicted"].add(field)
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


def derive_document_flags(patch):
    """An unconditional required CAS / I-20 document sets cas_required / i20_required.

    The document's verbatim quote becomes the field evidence, so the gate still
    checks it against the snapshot. Values the model already extracted win.
    """
    additions = {}
    for document in patch.documents:
        field = DOCUMENT_FLAGS.get(document.code.strip().casefold())
        if field and document.required and not document.condition and document.evidence_quote:
            additions.setdefault(field, document.evidence_quote)
    if not additions:
        return patch
    entries = list(patch.policies) or [VisaPolicyEntry(policy=VisaPolicyData(), evidence=[])]
    derived = []
    for entry in entries:
        fields = entry.policy.model_dump(exclude_none=True)
        evidence = list(entry.evidence)
        for field, quote in additions.items():
            if field in fields:
                continue
            fields[field] = True
            evidence.append(VisaEvidence(field=field, evidence_quote=quote,
                                         note="derived from required document"))
        derived.append(VisaPolicyEntry(policy=VisaPolicyData.model_validate(fields), evidence=evidence))
    return VisaPatch(policies=derived, documents=patch.documents)
