"""Cross-domain quality gate. Domain-specific gates live inside each workflow."""

from agent2.models import AnswerBody, Notice

from .contracts import DomainResult, QualityReport

GATE_VERSION = "orchestration-gate-1"


class DefaultQualityGate:
    version = GATE_VERSION

    async def check(self, results: list[DomainResult]) -> QualityReport:
        checked: list[DomainResult] = []
        findings: list[str] = []
        for result in results:
            if result.status == "answered" and not result.evidence_refs:
                findings.append(f"{result.task_id}: answered result has no evidence")
                result = result.model_copy(update={
                    "status": "needs_verification",
                    "answer": AnswerBody(
                        summary=result.answer.summary,
                        steps=result.answer.steps,
                        materials=result.answer.materials,
                        limitations=[*result.answer.limitations, "回答缺少可审计证据，已降级为待核实。"],
                    ),
                    "notices": [*result.notices,
                                Notice(code="missing_evidence", message="answered 结果必须带证据")],
                })
            checked.append(result)
        return QualityReport(results=checked, gate_version=self.version, findings=findings)
