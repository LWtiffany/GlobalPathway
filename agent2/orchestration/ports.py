"""Framework-neutral ports. Domain and persistence code should depend on these."""

from typing import Protocol

from .contracts import (DomainResult, DomainTask, LifecycleActions, OrchestrationInput,
                        QualityReport, TaskPlan, UnifiedAnswer)


class TaskPlanner(Protocol):
    version: str

    async def plan(self, request: OrchestrationInput) -> TaskPlan: ...


class DomainWorkflow(Protocol):
    async def run(self, task: DomainTask, request: OrchestrationInput) -> DomainResult: ...


class QualityGate(Protocol):
    version: str

    async def check(self, results: list[DomainResult]) -> QualityReport: ...


class ResponseComposer(Protocol):
    async def compose(self, plan: TaskPlan, report: QualityReport) -> UnifiedAnswer: ...


class LifecyclePlanner(Protocol):
    def propose(self, request: OrchestrationInput, plan: TaskPlan,
                answer: UnifiedAnswer) -> LifecycleActions: ...
