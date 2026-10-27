"""FX / remittance / overseas-spend workflow grounded on Agent1 FX policy quotes."""

from __future__ import annotations

import json
from agent2.fx_knowledge import (Agent1FxPolicyRead, FxPolicyBundle, FxPolicyLoader,
                                 PURPOSE_LABEL, build_fx_answer_parts)
from agent2.llm.contracts import StructuredModel
from agent2.llm.prompts import PromptRenderer
from agent2.llm.schemas import WrittenAnswer
from agent2.llm.validation import validate_written_answer
from agent2.models import AnswerBody, Claim, Citation, Material, Notice

from ..contracts import DomainResult, DomainTask, OrchestrationInput
from .minimal import COUNTRY_LABEL, merged_facts, needs_input

PROMPT_VERSION = "write-fx-answer-v1"
MODEL_VERSION_RULES = "fx-policy-rules-1"
REMITTER_COUNTRY = "CN"

_PURPOSE = {
    "remittance_tuition": "tuition",
    "remittance_living": "living",
}


def _limitations(*, mode: str, destination: str) -> list[str]:
    notes = [
        "依据为中国内地个人留学购汇工作底稿中的政策摘录，办理以银行/外汇局当时规则为准。",
        "不含实时汇率、手续费或可成交报价。",
        f"收款地按{destination}整理；若汇款人非中国内地居民，规则可能不同。",
    ]
    if mode == "overseas":
        notes.append("境外消费另受发卡行与当地支付网络规则约束，本回答只覆盖购汇额度与用途材料要点。")
    return notes


def _deterministic_body(*, place: str, purpose_zh: str, mode: str, facts: dict,
                        materials: list[Material]) -> AnswerBody:
    amount = facts.get("annual_quota_amount")
    currency = facts.get("annual_quota_currency") or "USD"
    remitter = facts.get("remitter_relation") or "按规定的汇款人关系"
    over = facts.get("over_quota_allowed")
    quota_line = (
        f"中国内地个人年度便利化购汇额度约为等值 {amount} {currency}。"
        if amount is not None else
        "请按银行公示的年度便利化购汇额度办理。"
    )
    steps = [
        f"确认向{place}支付{purpose_zh}，并准备收款方账户信息。",
        quota_line + (f"汇款人关系要求：{remitter}。" if remitter else ""),
    ]
    if over:
        steps.append("若超过年度便利化额度，需按超额度要求补充学费/在读/护照签证等证明材料后办理。")
    elif over is False:
        steps.append("额度内购汇按便利化流程办理；超额度政策未在当前资料中确认，需向办理银行核实。")
    else:
        steps.append("是否允许超额购汇以办理银行当时审核要求为准。")
    required = [m.name_zh for m in materials if m.applicability == "required"][:4]
    if required:
        steps.append("常用材料包括：" + "、".join(required) + "。")
    if mode == "overseas":
        summary = f"已结合中国内地购汇政策，整理赴{place}留学相关的换汇与境外消费准备要点。"
    else:
        summary = f"已结合中国内地留学购汇政策，整理汇往{place}的{purpose_zh}准备要点。"
    return AnswerBody(
        summary=summary,
        steps=[s for s in steps if s],
        materials=materials,
        limitations=_limitations(mode=mode, destination=place),
    )


def _claims_from_citations(citations: list[Citation], materials: list[Material]) -> list[Claim]:
    claims = []
    for citation in citations:
        mat = next((m for m in materials if m.citation_id == citation.citation_id), None)
        claims.append(Claim(
            claim_id=f"claim-{citation.citation_id.lower()}",
            text=citation.quote,
            risk="high",
            citation_ids=[citation.citation_id],
            requirement_key=mat.requirement_key if mat else None,
        ))
    return claims


class FxWorkflow:
    def __init__(self, *, model: StructuredModel | None = None,
                 policy_loader: FxPolicyLoader | None = None,
                 renderer: PromptRenderer | None = None,
                 remitter_country: str = REMITTER_COUNTRY):
        self.model = model
        self.policy_loader = policy_loader or Agent1FxPolicyRead().get
        self.renderer = renderer or PromptRenderer()
        self.remitter_country = remitter_country

    async def _draft_with_llm(self, *, request: OrchestrationInput, verified: AnswerBody,
                              policy_facts: dict) -> AnswerBody | None:
        if self.model is None:
            return None
        prompt = self.renderer.render(
            "write_fx_answer.jinja2",
            user_text=request.text,
            verified_json=json.dumps(policy_facts, ensure_ascii=False, sort_keys=True, default=str),
        )
        written = await self.model.parse(prompt=prompt, output_type=WrittenAnswer,
                                         operation="write_fx_answer")
        candidate = AnswerBody(
            summary=written.summary,
            steps=list(written.steps) or list(verified.steps),
            materials=verified.materials,
            limitations=verified.limitations,
        )
        validate_written_answer(candidate, verified)
        return candidate

    async def run(self, task: DomainTask, request: OrchestrationInput) -> DomainResult:
        facts, basis = merged_facts(task, request)
        intent = task.intent
        if intent in _PURPOSE and "remittance_purpose" not in facts:
            facts["remittance_purpose"] = _PURPOSE[intent]
            basis["remittance_purpose"] = PURPOSE_LABEL[_PURPOSE[intent]]
        if intent == "overseas_spending":
            facts["fx_mode"] = "overseas"
            basis.setdefault("fx_mode", "境外消费")
            purpose = facts.get("remittance_purpose") or "living"
        elif intent in _PURPOSE:
            purpose = facts.get("remittance_purpose") or _PURPOSE[intent]
            facts["fx_mode"] = purpose
            basis.setdefault("fx_mode", PURPOSE_LABEL.get(purpose, purpose))
        else:
            purpose = facts.get("remittance_purpose") or "tuition"
            facts["fx_mode"] = purpose

        missing = []
        if not facts.get("destination_country"):
            missing.append("destination_country")
        if intent in _PURPOSE and not facts.get("remittance_purpose"):
            missing.append("remittance_purpose")
        if missing:
            label = {
                "remittance_tuition": "学费汇款",
                "remittance_living": "生活费汇款",
                "overseas_spending": "境外消费与换汇",
            }.get(intent, "外汇与汇款")
            return needs_input(
                task, "fx",
                summary=f"还需要一些信息才能整理{label}准备事项。",
                missing=missing, facts=facts, basis=basis,
                profile_version=request.profile_version,
            )

        country = facts["destination_country"]
        place = COUNTRY_LABEL.get(country, country)
        mode = "overseas" if intent == "overseas_spending" else purpose
        purpose_code = purpose if purpose in PURPOSE_LABEL else "tuition"

        try:
            bundle: FxPolicyBundle | None = self.policy_loader(
                purpose_code=purpose_code, remitter_country=self.remitter_country)
        except TypeError:
            # Allow zero-arg test loaders that already close over purpose.
            bundle = self.policy_loader()  # type: ignore[call-arg]
        except Exception:
            bundle = None

        if bundle is None:
            return DomainResult(
                task_id=task.task_id,
                domain="fx",
                status="cannot_answer",
                answer=AnswerBody(
                    summary="暂时读不到可用的留学购汇政策资料，不能给出带出处的汇款建议。",
                    limitations=["外汇政策尚未发布或本地工作库不可用，请稍后重试。"],
                ),
                observed_facts=facts,
                observed_basis=basis,
                notices=[Notice(code="fx_policy_missing",
                                message="Agent1 FX_POLICY 工作表无匹配记录。")],
                model_version=MODEL_VERSION_RULES,
            )

        citations, materials, quotes, policy_facts = build_fx_answer_parts(
            bundle, destination_label=place, mode=mode)
        if not citations:
            return DomainResult(
                task_id=task.task_id,
                domain="fx",
                status="needs_verification",
                answer=AnswerBody(
                    summary="已找到留学购汇政策条目，但缺少可引用的原文摘录，暂不能给出确定建议。",
                    limitations=["需补充 FX_EVIDENCE 原文后再回答。"],
                ),
                observed_facts=facts,
                observed_basis=basis,
                evidence_refs=[bundle.policy_id],
                payload={"policy_id": bundle.policy_id},
                model_version=MODEL_VERSION_RULES,
            )

        purpose_zh = PURPOSE_LABEL.get(purpose_code, purpose_code)
        verified = _deterministic_body(
            place=place, purpose_zh=purpose_zh, mode=mode,
            facts=policy_facts, materials=materials,
        )
        body = verified
        model_version = MODEL_VERSION_RULES
        notices: list[Notice] = []
        try:
            drafted = await self._draft_with_llm(
                request=request, verified=verified, policy_facts=policy_facts)
            if drafted is not None:
                body = drafted
                model_version = f"{self.model.model_name}+{PROMPT_VERSION}"
        except Exception:
            notices.append(Notice(
                code="fx_llm_fallback",
                message="模型未能完成本轮外汇表述，已使用政策摘录与确定性要点。",
            ))

        claims = _claims_from_citations(citations, materials)
        return DomainResult(
            task_id=task.task_id,
            domain="fx",
            status="answered",
            answer=body,
            claims=claims,
            citations=citations,
            evidence_refs=[bundle.policy_id, *[c.evidence_id for c in citations[:6]]],
            next_actions=[
                f"核对汇往{place}的{purpose_zh}收款信息",
                "按额度内/超额准备对应材料并保留购汇凭证",
            ],
            notices=notices,
            observed_facts=facts,
            observed_basis=basis,
            payload={
                "fx_mode": mode,
                "destination_country": country,
                "remittance_purpose": purpose_code,
                "policy_id": bundle.policy_id,
                "policy_title": bundle.title,
                "quotes": quotes,
                "materials": [m.model_dump(mode="json") for m in materials],
                "citations": [c.model_dump(mode="json") for c in citations],
            },
            knowledge_read_id=f"agent1-fx-working:{bundle.environment}",
            model_version=model_version,
        )
