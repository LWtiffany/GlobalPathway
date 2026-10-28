from agent2.models import AnswerBody, Notice

from ..contracts import Domain, DomainResult, DomainTask


def todo_result(task: DomainTask, domain: Domain, label: str) -> DomainResult:
    return DomainResult(
        task_id=task.task_id,
        domain=domain,
        status="cannot_answer",
        answer=AnswerBody(
            summary=f"{label}流程尚未接入 Agent2 v2，当前不生成确定建议。",
            limitations=["领域规则、知识来源和质量门控仍待设计。"],
        ),
        next_actions=[f"完成{label}领域流程设计后接入此处理器。"],
        notices=[Notice(code="domain_workflow_todo", message=f"{domain} workflow TODO")],
    )
