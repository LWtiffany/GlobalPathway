from ..contracts import DomainResult, DomainTask, OrchestrationInput
from .base import todo_result


class BankAccountWorkflow:
    async def run(self, task: DomainTask, request: OrchestrationInput) -> DomainResult:
        # TODO(agent2-v2): define account eligibility, required documents and product comparison rules.
        return todo_result(task, "bank_account", "银行开户")
