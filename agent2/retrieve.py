"""M08 检索、适用性判定与冲突分组（S1：结构化过滤，不做向量召回）。

适用性只由确定性谓词决定：条件全部成立 → required；任一不成立 → 不适用（不列出）；
有字段未知 → conditional（待确认，列出缺的字段）；知识本身标为 unknown → unverified。
同一材料在同范围的可用记录间要求不同 → 冲突，全部保留，不任取一个。
"""
from collections import Counter
from dataclasses import dataclass, field
from urllib.parse import urlparse

from .models import Citation, Conflict, ReadRecord


@dataclass
class Item:
    read: ReadRecord
    requirement: object
    applicability: str  # required | conditional | unverified
    pending_fields: list
    citation: Citation


@dataclass
class EvidencePackage:
    items: list = field(default_factory=list)
    conflicts: list = field(default_factory=list)
    unavailable: list = field(default_factory=list)  # [(record_id, revision_id, reason)]
    condition_fields: list = field(default_factory=list)

    def citation(self, citation_id):
        return next((i.citation for i in self.items if i.citation.citation_id == citation_id), None)

    def item_for(self, citation_id):
        return next((i for i in self.items if i.citation.citation_id == citation_id), None)


_OPS = {
    "eq": lambda a, b: a == b, "ne": lambda a, b: a != b,
    "lt": lambda a, b: a < b, "lte": lambda a, b: a <= b,
    "gt": lambda a, b: a > b, "gte": lambda a, b: a >= b,
    "in": lambda a, b: a in b,
    "is_true": lambda a, _: a is True, "is_false": lambda a, _: a is False,
}


def condition_holds(condition, facts):
    """True / False, or None when the fact it depends on is unknown."""
    if condition.field not in facts:
        return None
    try:
        return bool(_OPS[condition.op](facts[condition.field], condition.value))
    except TypeError:
        return None


def display_url(url):
    """example.invalid etc. are placeholders and must never be shown as a real source."""
    host = urlparse(url).hostname or ""
    return None if host == "invalid" or host.endswith(".invalid") else url


def _citation(read, requirement, citation_id):
    record = read.record
    source = requirement.source or record.source
    return Citation(citation_id=citation_id, record_id=record.record_id, revision_id=record.revision_id,
                    evidence_id=requirement.evidence_id, source_name=source.name,
                    source_url=display_url(source.url), quote=requirement.evidence_quote,
                    condition_quote=requirement.condition_quote,
                    verified_at=record.verified_at, synthetic=record.synthetic)


def _signature(requirement):
    groups = [[c.model_dump(exclude={"text"}) for c in group] for group in requirement.alternatives()]
    return requirement.required, requirement.applicability_status, groups


def _evaluate(requirement, facts):
    """(holds, pending_fields): True if any group holds, False if every group fails, else None."""
    pending, undecided = [], False
    for group in requirement.alternatives():
        results = [(c, condition_holds(c, facts)) for c in group]
        if any(r is False for _, r in results):
            continue
        missing = [c.field for c, r in results if r is None]
        if not missing:
            return True, []
        undecided = True
        pending += [f for f in missing if f not in pending]
    return (None, pending) if undecided else (False, [])


def retrieve(read_records, facts):
    package = EvidencePackage()
    pending_counter = Counter()
    by_code = {}
    for read in read_records:
        if not read.available:
            package.unavailable.append((read.record.record_id, read.record.revision_id,
                                        read.availability_reason))
            continue
        for requirement in read.record.requirements:
            by_code.setdefault(requirement.document_code, []).append((read, requirement))

    seq = 0
    for code, variants in by_code.items():
        if len({str(_signature(r)) for _, r in variants}) > 1:
            conflict_variants = []
            for read, requirement in variants:
                seq += 1
                citation = _citation(read, requirement, f"s{seq}")
                package.items.append(Item(read, requirement, "conflict", [], citation))
                conflict_variants.append({
                    "required": requirement.required,
                    "applicability_status": requirement.applicability_status,
                    "condition": requirement.condition_text(),
                    "citation_id": citation.citation_id, "source_name": citation.source_name})
            package.conflicts.append(Conflict(document_code=code, name_zh=variants[0][1].name_zh,
                                              variants=conflict_variants))
            continue
        read, requirement = variants[0]
        if not requirement.required:
            continue
        if requirement.applicability_status == "unknown" or (
                requirement.applicability_status == "conditional" and not requirement.alternatives()):
            applicability, pending = "unverified", []
        elif requirement.applicability_status == "unconditional":
            applicability, pending = "required", []
        else:
            holds, pending = _evaluate(requirement, facts)
            if holds is False:
                continue
            applicability = "conditional" if pending else "required"
        seq += 1
        package.items.append(Item(read, requirement, applicability, pending,
                                  _citation(read, requirement, f"s{seq}")))
        pending_counter.update(dict.fromkeys(pending, 1))
    order = {f: i for i, f in enumerate(pending_counter)}
    package.condition_fields = sorted(pending_counter, key=lambda f: (-pending_counter[f], order[f]))
    return package
