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
    name = f"{material.name_zh}（{material.name_en}）"
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
                condition_text="且".join(c.text for c in requirement.condition or []) or None,
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
        summary = f"{place} {visa}：按你目前的条件，确定需要准备 {len(required)} 项材料"
        if conditional:
            summary += f"，另有 {len(conditional)} 项取决于尚未确认的条件"
        summary += "。"
        steps = ["对照下方「确定需要」的材料逐项准备，每项都可展开查看来源原文。"]
        if conditional:
            steps.append("补充下方追问的条件后，再确认「视条件而定」的材料是否需要。")
        steps.append("提交申请前，到官方入口核对最新要求（本回答的资料核验时间见每条引用）。")
        return Draft(summary=summary, steps=steps, materials=materials, claims=claims)
