from ..contracts import DomainResult, DomainTask, OrchestrationInput
from .base import todo_result


class CreditCardWorkflow:
    async def run(self, task: DomainTask, request: OrchestrationInput) -> DomainResult:
        # TODO(agent2-v2): define card eligibility, product data, ranking and domain gate.
        return todo_result(task, "credit_card", "信用卡")
