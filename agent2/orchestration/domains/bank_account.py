"""HSBC local bank-account opening: catalog products + LLM process copy."""

from __future__ import annotations

import json

from agent2.hsbc_account_knowledge import (IDENTITY_LABEL, REGION_LABEL, HsbcAccountBundle,
                                           build_account_bundle, verified_payload)
from agent2.llm.contracts import StructuredModel
from agent2.llm.prompts import PromptRenderer
from agent2.llm.schemas import WrittenAnswer
from agent2.llm.validation import validate_written_answer
from agent2.models import AnswerBody, Claim, Notice

from ..contracts import DomainResult, DomainTask, OrchestrationInput
from .minimal import merged_facts, needs_input

PROMPT_VERSION = "write-bank-answer-v1"
MODEL_VERSION_RULES = "hsbc-account-catalog-1"

_REGION_FROM_DEST = {"UK": "UK", "US": "US", "CN-HK": "CN-HK"}


def _limitations(place: str) -> list[str]:
    return [
        f"本回答只覆盖汇丰在{place}当地提供的账户产品与公开开户流程，不构成审批承诺。",
        "资格、材料、费用与渠道以汇丰官网/网点办理时展示为准，请自行核实。",
        "不推荐其他银行账户。",
    ]


def _deterministic_body(bundle: HsbcAccountBundle) -> AnswerBody:
    place = bundle.region_zh
    primary = bundle.products[0]
    eligibility = "；".join(primary.eligibility)
    steps = [
        f"开户地区：{place}；申请人：{bundle.identity_zh}；本回答仅推荐汇丰当地账户。",
        f"优先了解：{primary.name_zh}（{primary.name_en}）。{primary.summary}",
        f"公开资格要点：{eligibility}。",
    ]
    steps.extend(primary.process_steps[:4])
    if len(bundle.products) > 1:
        alt = bundle.products[1]
        steps.append(f"备选汇丰产品：{alt.name_zh}——{alt.summary}")
    return AnswerBody(
        summary=f"按{place}开户需求，建议优先了解汇丰「{primary.name_zh}」并按其公开流程准备。",
        steps=steps,
        materials=bundle.materials,
        limitations=_limitations(place),
    )


class BankAccountWorkflow:
    def __init__(self, *, model: StructuredModel | None = None,
                 renderer: PromptRenderer | None = None,
                 bundle_loader=None):
        self.model = model
        self.renderer = renderer or PromptRenderer()
        # Explicit None from the workflow factory must not wipe the default catalog.
        self.bundle_loader = bundle_loader or build_account_bundle

    async def _draft_with_llm(self, *, request: OrchestrationInput, verified: AnswerBody,
                              payload: dict) -> AnswerBody | None:
        if self.model is None:
            return None
        prompt = self.renderer.render(
            "write_bank_answer.jinja2",
            user_text=request.text,
            verified_json=json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str),
        )
        written = await self.model.parse(prompt=prompt, output_type=WrittenAnswer,
                                         operation="write_bank_answer")
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
        if not facts.get("account_region") and facts.get("destination_country") in _REGION_FROM_DEST:
            dest = facts["destination_country"]
            facts["account_region"] = _REGION_FROM_DEST[dest]
            basis.setdefault("account_region", basis.get("destination_country", dest))
        # Profile role already answers「谁开户」for the common student/parent paths.
        if not facts.get("applicant_identity") and facts.get("role") in IDENTITY_LABEL:
            role = facts["role"]
            facts["applicant_identity"] = role
            basis.setdefault("applicant_identity", IDENTITY_LABEL[role])

        missing = []
        if not facts.get("account_region"):
            missing.append("account_region")
        if not facts.get("applicant_identity"):
            missing.append("applicant_identity")
        if missing:
            return needs_input(
                task, "bank_account",
                summary="还需要一些信息才能判断适合的汇丰当地开户方案。",
                missing=missing, facts=facts, basis=basis,
                profile_version=request.profile_version,
            )

        region = facts["account_region"]
        identity = facts.get("applicant_identity") or "student"
        facts["applicant_identity"] = identity
        basis.setdefault("applicant_identity", IDENTITY_LABEL.get(identity, identity))
        place = REGION_LABEL.get(region, region)

        try:
            bundle = self.bundle_loader(region=region, identity=identity)
        except Exception:
            bundle = None

        if bundle is None or not bundle.products:
            return DomainResult(
                task_id=task.task_id,
                domain="bank_account",
                status="cannot_answer",
                answer=AnswerBody(
                    summary=f"暂未收录汇丰在{place}的可推荐开户产品，不能给出确定建议。",
                    limitations=["本服务目前覆盖英国、美国、中国香港的汇丰当地开户指引。"],
                ),
                observed_facts=facts,
                observed_basis=basis,
                notices=[Notice(code="hsbc_account_missing",
                                message=f"无 {region} 汇丰账户产品目录。")],
                model_version=MODEL_VERSION_RULES,
            )

        payload = verified_payload(bundle)
        payload["user_need"] = request.text
        verified = _deterministic_body(bundle)
        body = verified
        model_version = MODEL_VERSION_RULES
        notices: list[Notice] = []
        try:
            drafted = await self._draft_with_llm(
                request=request, verified=verified, payload=payload)
            if drafted is not None:
                body = drafted
                model_version = f"{self.model.model_name}+{PROMPT_VERSION}"
        except Exception:
            notices.append(Notice(
                code="bank_llm_fallback",
                message="模型未能完成本轮开户表述，已使用汇丰产品目录与公开流程要点。",
            ))

        claims = [
            Claim(claim_id=f"claim-{c.citation_id.lower()}", text=c.quote, risk="high",
                  citation_ids=[c.citation_id])
            for c in bundle.citations
        ]
        primary = bundle.products[0]
        return DomainResult(
            task_id=task.task_id,
            domain="bank_account",
            status="answered",
            answer=body,
            claims=claims,
            citations=bundle.citations,
            evidence_refs=[p.product_id for p in bundle.products],
            next_actions=[
                f"打开汇丰官网核对「{primary.name_zh}」资格与材料",
                "按公开流程提交申请或预约网点",
            ],
            notices=notices,
            observed_facts=facts,
            observed_basis=basis,
            payload={
                "account_region": region,
                "applicant_identity": identity,
                "account_product": primary.product_id,
                "products": payload["products"],
                "materials": [m.model_dump(mode="json") for m in bundle.materials],
                "citations": [c.model_dump(mode="json") for c in bundle.citations],
            },
            knowledge_read_id="hsbc-account-catalog-1",
            model_version=model_version,
        )
