"""Typed Agent2 v2 graph: plan -> domains -> gate -> compose -> snapshot -> projections."""

import asyncio
import re
import time
import uuid
from dataclasses import dataclass, field

from pydantic_graph import GraphBuilder, StepContext

from agent2 import clarify
from agent2.llm.contracts import AnswerWriterPort, ConversationReasonerPort, UserUnderstandingPort
from agent2.llm.validation import validate_understanding, validate_written_answer
from agent2.models import Notice

from .composer import DefaultResponseComposer
from .capabilities import RuleCapabilityPlanner
from .contracts import (AnswerSnapshot, CapabilityAction, Domain, DomainResult, LifecycleActions, OrchestrationInput,
                        OrchestrationOutput, QualityReport, TaskPlan, UnifiedAnswer)
from .domains import default_domain_workflows
from .lifecycle import DefaultLifecyclePlanner
from .planner import RuleTaskPlanner
from .ports import (CapabilityPlanner, DomainWorkflow, LifecyclePlanner, QualityGate,
                    ResponseComposer, TaskPlanner)
from .quality import DefaultQualityGate


_INTERNAL_OUTPUT_PATTERNS = (
    re.compile(r"\b(?:prompt|completion)\s*\d+", re.I),
    re.compile(r"\b(?:evidence_ids?|source_ids?|field_code|trace_id|prompt_version)\b", re.I),
    re.compile(r"\b(?:llm|gateway|model|token(?:s)?|completion|timeout)\b", re.I),
    re.compile(r"\banswered\s+(?:result|results)\b|\bmust\s+(?:include|have|contain)\s+evidence\b|\bmissing\s+evidence\b|结果必须带证据|缺少可审计证据", re.I),
    re.compile(r"只在\s*(?:evidence|证据)|内部(?:字段|指令|提示)|不要向用户|不可展示", re.I),
    re.compile(r"\bs\d+(?:\s*[,，、;；]\s*s\d+)*\b", re.I),
)
_INTERNAL_NOTICE_CODES = {
    "missing_evidence", "llm_fallback", "domain_workflow_todo", "intent_routing",
    "llm_routing_low_confidence", "unsupported_intent", "partial_out_of_scope",
}


def _looks_internal_output(text: str) -> bool:
    value = str(text or "").strip()
    return bool(value) and any(pattern.search(value) for pattern in _INTERNAL_OUTPUT_PATTERNS)


def _sanitize_user_answer(answer: UnifiedAnswer) -> UnifiedAnswer:
    body = answer.answer
    clean_steps = [line for line in body.steps if not _looks_internal_output(line)]
    clean_limitations = [line for line in body.limitations if not _looks_internal_output(line)]
    clean_actions = [line for line in answer.next_actions if not _looks_internal_output(line)]
    clean_notices = [notice for notice in answer.notices
                     if notice.code not in _INTERNAL_NOTICE_CODES
                     and not _looks_internal_output(notice.message)]
    summary = body.summary if not _looks_internal_output(body.summary) else "当前资料不足以支持确定回答，请按页面提示补充信息或核实官方要求。"
    if not clean_steps and body.steps:
        clean_steps = ["当前资料不足以支持确定回答，请核实适用条件和官方要求。"]
    return answer.model_copy(update={
        "answer": body.model_copy(update={
            "summary": summary,
            "steps": clean_steps,
            "limitations": clean_limitations,
        }),
        "domain_results": [result.model_copy(update={
            "answer": _sanitize_user_answer(UnifiedAnswer(
                status=result.status, answer=result.answer,
                next_actions=result.next_actions, notices=result.notices,
                follow_up_questions=result.follow_up_questions)).answer,
            "next_actions": [line for line in result.next_actions if not _looks_internal_output(line)],
            "notices": [notice for notice in result.notices
                        if notice.code not in _INTERNAL_NOTICE_CODES
                        and not _looks_internal_output(notice.message)],
            "follow_up_questions": _sanitize_user_answer(UnifiedAnswer(
                status=result.status, answer=result.answer,
                follow_up_questions=result.follow_up_questions)).follow_up_questions,
        }) for result in answer.domain_results],
        "next_actions": clean_actions,
        "notices": clean_notices,
        "follow_up_questions": [question.model_copy(update={
            "text": question.text if not _looks_internal_output(question.text)
            else "请补充相关信息，我会继续为您核对。",
            "options": [option for option in question.options
                        if not _looks_internal_output(option.get("label", ""))
                        and not _looks_internal_output(option.get("message", ""))],
        }) for question in answer.follow_up_questions],
    })


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
    llm_model_versions: dict[str, str] = field(default_factory=dict)
    llm_prompt_versions: dict[str, str] = field(default_factory=dict)
    llm_failures: list[str] = field(default_factory=list)


@dataclass
class WorkflowDependencies:
    planner: TaskPlanner
    capability_planner: CapabilityPlanner
    domains: dict[Domain, DomainWorkflow]
    gate: QualityGate
    composer: ResponseComposer
    lifecycle: LifecyclePlanner
    understanding: UserUnderstandingPort | None = None
    writer: AnswerWriterPort | None = None
    reasoner: ConversationReasonerPort | None = None


builder = GraphBuilder(
    name="agent2-v2",
    state_type=WorkflowState,
    deps_type=WorkflowDependencies,
    input_type=OrchestrationInput,
    output_type=OrchestrationOutput,
)


@builder.step(label="理解自由文本")
async def understand_user(ctx: StepContext[WorkflowState, WorkflowDependencies, OrchestrationInput]) -> None:
    started = time.perf_counter()
    ctx.state.request = ctx.inputs
    port = ctx.deps.understanding
    if ctx.inputs.understanding_applied:
        # Service already merged rule∪LLM intents for routing; reuse without a second call.
        if port is not None and ctx.inputs.understanding_model_used:
            ctx.state.llm_model_versions["understanding"] = port.model_version
            ctx.state.llm_prompt_versions["understanding"] = port.prompt_version
    elif (port is not None and ctx.inputs.input_mode == "free_text"
          and ctx.inputs.text.strip()):
        try:
            catalog = clarify.field_catalog()
            raw = await port.understand(ctx.inputs, catalog)
            facts, basis, intents, topic_switch = validate_understanding(
                raw, ctx.inputs, set(catalog), clarify.validate)
            ctx.state.request = ctx.inputs.model_copy(update={
                "conversation_intents": list(dict.fromkeys([
                    *ctx.inputs.conversation_intents, *intents])),
                "llm_intents": list(dict.fromkeys([
                    *ctx.inputs.llm_intents, *intents])),
                "understood_facts": facts,
                "understood_basis": basis,
                "llm_topic_switch": topic_switch,
                "understanding_applied": True,
            })
            ctx.state.llm_model_versions["understanding"] = port.model_version
            ctx.state.llm_prompt_versions["understanding"] = port.prompt_version
        except Exception as exc:  # deterministic extraction remains available
            ctx.state.llm_failures.append(f"understanding:{type(exc).__name__}")
    ctx.state.stage_ms["understood"] = round((time.perf_counter() - started) * 1000, 3)


@builder.step(label="多意图规划")
async def plan_tasks(ctx: StepContext[WorkflowState, WorkflowDependencies, None]) -> None:
    started = time.perf_counter()
    assert ctx.state.request is not None
    ctx.state.plan = await ctx.deps.planner.plan(ctx.state.request)
    ctx.state.stage_ms["planned"] = round((time.perf_counter() - started) * 1000, 3)


@builder.step(label="领域能力规划")
async def plan_capabilities(ctx: StepContext[WorkflowState, WorkflowDependencies, None]) -> None:
    started = time.perf_counter()
    assert ctx.state.request is not None and ctx.state.plan is not None
    ctx.state.plan = await ctx.deps.capability_planner.enrich(ctx.state.plan, ctx.state.request)
    ctx.state.stage_ms["capabilities_planned"] = round(
        (time.perf_counter() - started) * 1000, 3)


@builder.step(label="对话下一步判断")
async def reason_about_turn(ctx: StepContext[WorkflowState, WorkflowDependencies, None]) -> None:
    started = time.perf_counter()
    request, plan, reasoner = ctx.state.request, ctx.state.plan, ctx.deps.reasoner
    assert request is not None and plan is not None
    # The model cannot change an action backed by an explicit rule match.
    # Avoid a serial model call when it has no decision left to make.
    if (reasoner is not None and request.input_mode == "free_text" and plan.tasks
            and any(not any(action.evidence for action in task.actions)
                    for task in plan.tasks)):
        try:
            proposed = await reasoner.decide(request, plan)
            decisions = {d.task_id: d for d in proposed.decisions}
            revised = []
            for task in plan.tasks:
                decision = decisions.get(task.task_id)
                # Explicit rule evidence wins. Model choices refine only defaults and
                # elliptical follow-ups, never route or mutate profile data.
                explicit = any(action.evidence for action in task.actions)
                valid = (decision is not None and decision.confidence >= .8
                         and bool(decision.evidence)
                         and decision.evidence.casefold() in request.text.casefold()
                         and not explicit)
                if valid:
                    action = CapabilityAction(
                        action_id=f"{task.task_id}-reasoned", operation=decision.operation,
                        confidence=decision.confidence, evidence=decision.evidence)
                    task = task.model_copy(update={"actions": [action]})
                revised.append(task)
            ctx.state.plan = plan.model_copy(update={
                "tasks": revised, "planner_version": f"{plan.planner_version}+reasoner-1"})
            ctx.state.llm_model_versions["reasoner"] = reasoner.model_version
            ctx.state.llm_prompt_versions["reasoner"] = reasoner.prompt_version
        except Exception as exc:
            ctx.state.llm_failures.append(f"reasoner:{type(exc).__name__}")
    ctx.state.stage_ms["reasoned"] = round((time.perf_counter() - started) * 1000, 3)


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
    user_text = ctx.state.request.text if ctx.state.request is not None else ""
    ctx.state.answer = await ctx.deps.composer.compose(
        ctx.state.plan, ctx.state.quality_report, user_text=user_text)
    ctx.state.stage_ms["composed"] = round((time.perf_counter() - started) * 1000, 3)


@builder.step(label="LLM建议表达")
async def write_answer(ctx: StepContext[WorkflowState, WorkflowDependencies, None]) -> None:
    started = time.perf_counter()
    assert ctx.state.request is not None and ctx.state.answer is not None
    port = ctx.deps.writer
    direct_card_answer = (len(ctx.state.answer.domain_results) == 1
                          and ctx.state.answer.domain_results[0].domain == "credit_card"
                          and ctx.state.answer.domain_results[0].payload.get("capability")
                          in {"product_qa", "general_knowledge"})
    if (port is not None and not direct_card_answer
            and ctx.state.answer.status in {"answered", "needs_verification"}):
        try:
            body = await port.write(request=ctx.state.request, verified_answer=ctx.state.answer)
            validate_written_answer(body, ctx.state.answer.answer,
                                    user_text=ctx.state.request.text,
                                    profile_name=ctx.state.request.profile_name)
            ctx.state.answer = ctx.state.answer.model_copy(update={"answer": body})
            ctx.state.llm_model_versions["writer"] = port.model_version
            ctx.state.llm_prompt_versions["writer"] = port.prompt_version
        except Exception as exc:  # preserve the already gated deterministic answer
            ctx.state.llm_failures.append(f"writer:{type(exc).__name__}")
    if ctx.state.llm_failures:
        existing = {notice.code for notice in ctx.state.answer.notices}
        if "llm_fallback" not in existing:
            ctx.state.answer = ctx.state.answer.model_copy(update={
                "notices": [*ctx.state.answer.notices, Notice(
                    code="llm_fallback",
                    message="模型服务未完成本轮处理，已使用确定性规则和模板结果。")],
            })
    ctx.state.stage_ms["written"] = round((time.perf_counter() - started) * 1000, 3)


@builder.step(label="冻结回答并生成后续动作")
async def finalize(ctx: StepContext[WorkflowState, WorkflowDependencies, None]) -> OrchestrationOutput:
    started = time.perf_counter()
    assert ctx.state.request is not None and ctx.state.plan is not None
    assert ctx.state.answer is not None and ctx.state.quality_report is not None
    ctx.state.answer = _sanitize_user_answer(ctx.state.answer)
    read_ids = {item.task_id: item.knowledge_read_id for item in ctx.state.domain_results
                if item.knowledge_read_id}
    model_versions = {item.task_id: item.model_version for item in ctx.state.domain_results}
    model_versions.update(ctx.state.llm_model_versions)
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
        prompt_versions=ctx.state.llm_prompt_versions,
    )
    ctx.state.lifecycle = ctx.deps.lifecycle.propose(ctx.state.request, ctx.state.plan, ctx.state.answer)
    ctx.state.stage_ms["finalized"] = round((time.perf_counter() - started) * 1000, 3)
    return OrchestrationOutput(trace_id=ctx.state.trace_id, answer=ctx.state.answer,
                               snapshot=ctx.state.snapshot, lifecycle=ctx.state.lifecycle,
                               stage_ms=ctx.state.stage_ms)


builder.add(
    builder.edge_from(builder.start_node).to(understand_user),
    builder.edge_from(understand_user).to(plan_tasks),
    builder.edge_from(plan_tasks).to(plan_capabilities),
    builder.edge_from(plan_capabilities).to(reason_about_turn),
    builder.edge_from(reason_about_turn).to(run_domains),
    builder.edge_from(run_domains).to(gate_results),
    builder.edge_from(gate_results).to(compose_answer),
    builder.edge_from(compose_answer).to(write_answer),
    builder.edge_from(write_answer).to(finalize),
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


def build_default_workflow(*, credit_card_repository=None, understanding=None, writer=None, reasoner=None,
                           fx_model=None, fx_policy_loader=None,
                           bank_model=None, bank_repository=None,
                           visa_knowledge=None, clock=None, visa_generator=None) -> Agent2Workflow:
    # Prefer an explicit FX model (GLM-5.3 thinking) over shared writer/understanding.
    shared = bank_model
    if shared is None and writer is not None:
        shared = getattr(writer, "model", None)
    if shared is None and understanding is not None:
        shared = getattr(understanding, "model", None)
    resolved_fx = fx_model or shared
    resolved_bank = bank_model or shared
    return Agent2Workflow(WorkflowDependencies(
        planner=RuleTaskPlanner(),
        capability_planner=RuleCapabilityPlanner(),
        domains=default_domain_workflows(
            credit_card_repository=credit_card_repository,
            fx_model=resolved_fx,
            fx_policy_loader=fx_policy_loader,
            bank_model=resolved_bank,
            bank_repository=bank_repository,
            visa_knowledge=visa_knowledge,
            clock=clock,
            visa_generator=visa_generator,
        ),
        gate=DefaultQualityGate(),
        composer=DefaultResponseComposer(),
        lifecycle=DefaultLifecyclePlanner(),
        understanding=understanding,
        writer=writer,
        reasoner=reasoner,
    ))
