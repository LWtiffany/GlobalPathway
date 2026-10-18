"""Typed Agent2 v2 graph: plan -> domains -> gate -> compose -> snapshot -> projections."""

import asyncio
import time
import uuid
from dataclasses import dataclass, field

from pydantic_graph import GraphBuilder, StepContext

from .composer import DefaultResponseComposer
from .contracts import (AnswerSnapshot, Domain, DomainResult, LifecycleActions, OrchestrationInput,
                        OrchestrationOutput, QualityReport, TaskPlan, UnifiedAnswer)
from .domains import default_domain_workflows
from .lifecycle import DefaultLifecyclePlanner
from .planner import RuleTaskPlanner
from .ports import DomainWorkflow, LifecyclePlanner, QualityGate, ResponseComposer, TaskPlanner
from .quality import DefaultQualityGate


@dataclass
class WorkflowState:
    trace_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    request: OrchestrationInput | None = None
    plan: TaskPlan | None = None
    domain_results: list[DomainResult] = field(default_factory=list)
    quality_report: QualityReport | None = None
    answer: UnifiedAnswer | None = None
    snapshot: AnswerSnapshot | None = None
    lifecycle: LifecycleActions | None = None
    stage_ms: dict[str, float] = field(default_factory=dict)


@dataclass
class WorkflowDependencies:
    planner: TaskPlanner
    domains: dict[Domain, DomainWorkflow]
    gate: QualityGate
    composer: ResponseComposer
    lifecycle: LifecyclePlanner


builder = GraphBuilder(
    name="agent2-v2",
    state_type=WorkflowState,
    deps_type=WorkflowDependencies,
    input_type=OrchestrationInput,
    output_type=OrchestrationOutput,
)


@builder.step(label="多意图规划")
async def plan_tasks(ctx: StepContext[WorkflowState, WorkflowDependencies, OrchestrationInput]) -> None:
    started = time.perf_counter()
    ctx.state.request = ctx.inputs
    ctx.state.plan = await ctx.deps.planner.plan(ctx.inputs)
    ctx.state.stage_ms["planned"] = round((time.perf_counter() - started) * 1000, 3)


@builder.step(label="执行领域流程")
async def run_domains(ctx: StepContext[WorkflowState, WorkflowDependencies, None]) -> None:
    started = time.perf_counter()
    assert ctx.state.request is not None and ctx.state.plan is not None
    # TODO(agent2-v2): once domain dependencies are explicit, run independent tasks concurrently.
    for task in ctx.state.plan.tasks:
        workflow = ctx.deps.domains[task.domain]
        ctx.state.domain_results.append(await workflow.run(task, ctx.state.request))
    ctx.state.stage_ms["domains"] = round((time.perf_counter() - started) * 1000, 3)


@builder.step(label="质量门控")
async def gate_results(ctx: StepContext[WorkflowState, WorkflowDependencies, None]) -> None:
    started = time.perf_counter()
    ctx.state.quality_report = await ctx.deps.gate.check(ctx.state.domain_results)
    ctx.state.stage_ms["gated"] = round((time.perf_counter() - started) * 1000, 3)


@builder.step(label="统一回答")
async def compose_answer(ctx: StepContext[WorkflowState, WorkflowDependencies, None]) -> None:
    started = time.perf_counter()
    assert ctx.state.plan is not None and ctx.state.quality_report is not None
    ctx.state.answer = await ctx.deps.composer.compose(ctx.state.plan, ctx.state.quality_report)
    ctx.state.stage_ms["composed"] = round((time.perf_counter() - started) * 1000, 3)


@builder.step(label="冻结回答并生成后续动作")
async def finalize(ctx: StepContext[WorkflowState, WorkflowDependencies, None]) -> OrchestrationOutput:
    started = time.perf_counter()
    assert ctx.state.request is not None and ctx.state.plan is not None
    assert ctx.state.answer is not None and ctx.state.quality_report is not None
    read_ids = {item.task_id: item.knowledge_read_id for item in ctx.state.domain_results
                if item.knowledge_read_id}
    model_versions = {item.task_id: item.model_version for item in ctx.state.domain_results}
    # The snapshot is built first. Persistence adapters must write it before applying projections.
    ctx.state.snapshot = AnswerSnapshot(
        trace_id=ctx.state.trace_id,
        profile_id=ctx.state.request.profile_id,
        profile_version=ctx.state.request.profile_version,
        user_text=ctx.state.request.text,
        task_plan=ctx.state.plan,
        answer=ctx.state.answer,
        gate_version=ctx.state.quality_report.gate_version,
        knowledge_read_ids=read_ids,
        model_versions=model_versions,
    )
    ctx.state.lifecycle = ctx.deps.lifecycle.propose(ctx.state.request, ctx.state.plan, ctx.state.answer)
    ctx.state.stage_ms["finalized"] = round((time.perf_counter() - started) * 1000, 3)
    return OrchestrationOutput(trace_id=ctx.state.trace_id, answer=ctx.state.answer,
                               snapshot=ctx.state.snapshot, lifecycle=ctx.state.lifecycle,
                               stage_ms=ctx.state.stage_ms)


builder.add(
    builder.edge_from(builder.start_node).to(plan_tasks),
    builder.edge_from(plan_tasks).to(run_domains),
    builder.edge_from(run_domains).to(gate_results),
    builder.edge_from(gate_results).to(compose_answer),
    builder.edge_from(compose_answer).to(finalize),
    builder.edge_from(finalize).to(builder.end_node),
)
AGENT2_GRAPH = builder.build()


class Agent2Workflow:
    def __init__(self, dependencies: WorkflowDependencies):
        self.dependencies = dependencies

    async def run_async(self, request: OrchestrationInput) -> OrchestrationOutput:
        return await AGENT2_GRAPH.run(inputs=request, state=WorkflowState(), deps=self.dependencies)

    def run(self, request: OrchestrationInput) -> OrchestrationOutput:
        return asyncio.run(self.run_async(request))


def build_default_workflow(*, credit_card_repository=None) -> Agent2Workflow:
    return Agent2Workflow(WorkflowDependencies(
        planner=RuleTaskPlanner(),
        domains=default_domain_workflows(credit_card_repository=credit_card_repository),
        gate=DefaultQualityGate(),
        composer=DefaultResponseComposer(),
        lifecycle=DefaultLifecyclePlanner(),
    ))
