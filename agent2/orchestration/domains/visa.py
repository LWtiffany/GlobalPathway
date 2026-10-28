from ..contracts import DomainResult, DomainTask, OrchestrationInput
from .base import todo_result


class VisaWorkflow:
    async def run(self, task: DomainTask, request: OrchestrationInput) -> DomainResult:
        # TODO(agent2-v2): define visa facts, official knowledge policy, retrieval and domain gate.
        return todo_result(task, "visa", "签证")
