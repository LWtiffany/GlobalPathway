"""Cross-domain quality gate. Domain-specific gates live inside each workflow."""

from agent2.models import AnswerBody

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
                        limitations=[*result.answer.limitations,
                                     "目前无法确认具体申请条件，建议核对发卡地区和卡种的最新要求。"],
                    ),
                })
            checked.append(result)
        return QualityReport(results=checked, gate_version=self.version, findings=findings)
