"""M10 确定性质量门控（S1）。

顺序固定：输出 Schema → 引用存在且属于本次证据包 → 知识仍可用（提交前复查修订）→
适用条件 → 数字 → 材料条件表述 → 来源冲突 → 最终状态。
不通过的材料连同它的 claim 一起阻断；核心材料（确定需要）全部被阻断则 cannot_answer。
"""
import re
from dataclasses import dataclass, field

from pydantic import ValidationError

from .models import AnswerBody, Claim

GATE_VERSION = "s1-gate-1"
_NUMBER = re.compile(r"\d+(?:\.\d+)?")


@dataclass
class GateResult:
    status: str  # answered | needs_verification | cannot_answer
    materials: list = field(default_factory=list)
    claims: list = field(default_factory=list)
    citations: list = field(default_factory=list)
    blocked: list = field(default_factory=list)  # [{"requirement_key", "reasons"}]
    limitations: list = field(default_factory=list)


def _material_problems(material, claims, package, knowledge, now):
    item = package.item_for(material.citation_id)
    if item is None or item.applicability == "conflict":
        return ["引用不在本次证据包中"]
    if item.requirement.requirement_key != material.requirement_key:
        return ["引用指向的材料与本条材料不符"]
    problems = []
    current = knowledge.get(item.citation.record_id, item.citation.revision_id, now=now)
    if current is None or not current.available:
        reason = current.availability_reason if current else "not_found"
        problems.append(f"引用的知识修订已不可用（{reason}）")
    if material.applicability != item.applicability:
        problems.append(f"适用性写成 {material.applicability}，判定结果为 {item.applicability}")
    if not claims:
        problems.append("材料没有对应的 claim")
    quote_numbers = set(_NUMBER.findall(item.citation.quote))
    condition_numbers = set(_NUMBER.findall(material.condition_text or ""))
    for claim in claims:
        if not claim.citation_ids or any(cid != material.citation_id for cid in claim.citation_ids):
            problems.append(f"{claim.claim_id} 的引用与材料引用不一致")
        extra = set(_NUMBER.findall(claim.text)) - quote_numbers - condition_numbers
        if extra:
            problems.append(f"{claim.claim_id} 含原文没有的数字：{', '.join(sorted(extra))}")
        if material.applicability == "conditional" and (
                not material.condition_text or material.condition_text not in claim.text):
            problems.append(f"{claim.claim_id} 没有写明适用条件，不能写成无条件需要")
    return problems


def run_gate(draft, package, knowledge, now):
    try:
        AnswerBody(summary=draft.summary, steps=draft.steps, materials=draft.materials,
                   limitations=draft.limitations)
        claims = [c if isinstance(c, Claim) else Claim.model_validate(c) for c in draft.claims]
    except ValidationError as exc:
        return GateResult(status="cannot_answer", blocked=[{"requirement_key": None,
                                                            "reasons": [f"输出结构不合法：{exc.error_count()} 处"]}])
    by_requirement = {}
    for claim in claims:
        by_requirement.setdefault(claim.requirement_key, []).append(claim)

    result = GateResult(status="answered")
    for material in draft.materials:
        mine = by_requirement.get(material.requirement_key, [])
        problems = _material_problems(material, mine, package, knowledge, now)
        if problems:
            result.blocked.append({"requirement_key": material.requirement_key, "reasons": problems})
            continue
        result.materials.append(material)
        result.claims.extend(mine)
    for claim in by_requirement.get(None, []):
        if claim.risk == "high":
            result.blocked.append({"requirement_key": None, "reasons": [f"{claim.claim_id} 是高风险结论但未关联材料"]})
    used = {cid for claim in result.claims for cid in claim.citation_ids}
    result.citations = [package.citation(cid) for cid in sorted(used, key=lambda c: int(c[1:]))]

    if result.blocked:
        result.limitations.append(f"{len(result.blocked)} 项内容未通过引用/适用性校验，未展示。")
    core_expected = any(m.applicability == "required" for m in draft.materials)
    core_kept = any(m.applicability == "required" for m in result.materials)
    if package.conflicts:
        result.status = "needs_verification"
    elif core_expected and not core_kept:
        result.status = "cannot_answer"
    return result
