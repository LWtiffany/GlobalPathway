from ..contracts import DomainResult, DomainTask, OrchestrationInput
from .base import todo_result


class FxWorkflow:
    async def run(self, task: DomainTask, request: OrchestrationInput) -> DomainResult:
        # TODO(agent2-v2): define FX/remittance quote, fee, limit and suitability rules.
        return todo_result(task, "fx", "外汇与汇款")
