"""M09 生成（S1：模板化）。

只把证据包里的结构化事实组织成中文说明：不新增金额、日期、资格或材料，英文专有名词保留
原文（S1-07），每条材料 claim 都带引用 ID。后续接模型时实现同一 Generator 协议，输出仍
必须经过 M10 门控。
"""
from dataclasses import dataclass, field
from typing import Protocol

from .models import Claim, Material

COUNTRY_ZH = {"UK": "英国", "US": "美国", "CN-HK": "中国香港"}
VISA_ZH = {"student_visa": "Student visa（学生签证）", "visitor_visa": "访问签证",
           "graduate_visa": "Graduate visa", "dependant_visa": "家属签证"}
MODEL_VERSION = "template-v1"
PROMPT_VERSION = "s1-template-1"


@dataclass
class Draft:
    summary: str
    steps: list = field(default_factory=list)
    materials: list = field(default_factory=list)
    claims: list = field(default_factory=list)
    limitations: list = field(default_factory=list)


class Generator(Protocol):
    model_version: str

    def draft(self, *, facts, package, assessment) -> Draft: ...


def _claim_text(material):
    name = f"{material.name_zh}（{material.name_en}）" if material.name_en else material.name_zh
    if material.applicability == "required":
        return f"需要提供{name}。"
    if material.applicability == "conditional":
        return f"如果{material.condition_text}，需要提供{name}。"
    return f"{name}是否需要，资料未说明完整的适用条件，待核实。"


class TemplateGenerator:
    model_version = MODEL_VERSION

    def draft(self, *, facts, package, assessment):
        materials, claims = [], []
        for item in package.items:
            if item.applicability == "conflict":
                continue
            requirement = item.requirement
            material = Material(
                requirement_key=requirement.requirement_key, document_code=requirement.document_code,
                name_zh=requirement.name_zh, name_en=requirement.name_en,
                applicability=item.applicability,
                condition_text=requirement.condition_text(),
                pending_fields=item.pending_fields, citation_id=item.citation.citation_id)
            materials.append(material)
            claims.append(Claim(claim_id=f"c{len(claims) + 1}", text=_claim_text(material),
                                risk="low" if material.applicability == "unverified" else "high",
                                citation_ids=[material.citation_id],
                                requirement_key=material.requirement_key))
        place = COUNTRY_ZH.get(facts.get("destination_country"), facts.get("destination_country"))
        visa = VISA_ZH.get(facts.get("visa_category"), facts.get("visa_category"))
        required = [m for m in materials if m.applicability == "required"]
        conditional = [m for m in materials if m.applicability == "conditional"]
        unverified = [m for m in materials if m.applicability == "unverified"]
        summary = f"{place} {visa}：按你目前的条件，需准备 {len(required)} 项必需材料"
        if conditional:
            summary += f"，另有 {len(conditional)} 项视情况而定"
        if unverified:
            summary += f"，{len(unverified)} 项待核实"
        summary += "。"
        steps = _prep_steps(required, conditional, unverified, assessment)
        return Draft(summary=summary, steps=steps, materials=materials, claims=claims)


def _names(materials, limit=5):
    shown = [m.name_zh for m in materials[:limit]]
    text = "、".join(shown)
    if len(materials) > limit:
        text += f"等 {len(materials)} 项"
    return text


def _prep_steps(required, conditional, unverified, assessment):
    """Only add prep lines that add information beyond the material list above.

    Wording matches UI tags（必需 / 视情况 / 待核实）. Pure「必需」lists leave
    steps empty so the chat does not show a generic「准备要点」block.
    """
    steps = []
    for material in conditional:
        if material.condition_text:
            steps.append(f"若{material.condition_text}，再准备{material.name_zh}。")
        elif material.pending_fields:
            labels = "、".join(material.pending_fields)
            steps.append(f"确认{labels}后，再判断是否需要{material.name_zh}。")
        else:
            steps.append(f"「{material.name_zh}」视个人情况而定，对照其条件说明。")
    if unverified:
        steps.append(
            f"「{_names(unverified)}」在资料中未写清适用条件，标为待核实，"
            "对照引用原文后再决定是否提交。")
    pending = list(getattr(assessment, "pending_fields", None) or [])
    if pending and not conditional and not unverified and required:
        steps.append(
            f"上方 {len(required)} 项「必需」可先备齐；回答追问后，视情况材料会再收窄。")
    return steps
