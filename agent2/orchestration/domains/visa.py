"""Visa workflow backed by the published knowledge read contract."""

from agent2 import clarify, gate as visa_gate
from agent2.generate import COUNTRY_ZH, VISA_ZH, TemplateGenerator
from agent2.models import AnswerBody, Notice, Profile, ProfileField
from agent2.retrieve import retrieve

from ..contracts import DomainResult, DomainTask, OrchestrationInput
from .minimal import merged_facts

MODEL_VERSION = "visa-evidence-workflow-1"

OFFICIAL_ENTRY = {
    ("UK", "student_visa"): "GOV.UK Student visa 官方页面（https://www.gov.uk/student-visa）",
    ("US", "student_visa"): "美国国务院学生签证页面（https://travel.state.gov/content/travel/en/us-visas/study/student-visa.html）"
                            "及面签所在使领馆网站",
    ("CN-HK", "student_visa"): "香港入境事务处来港就读页面（https://www.immd.gov.hk/eng/services/visas/study.html）",
}


def _next_check(facts):
    official = OFFICIAL_ENTRY.get((facts.get("destination_country"), facts.get("visa_category")))
    return [f"到{official}核实最新要求"] if official else ["到目的地官方签证网站核实最新要求"]


def _profile(request, facts):
    return Profile(profile_id=request.profile_id, user_id="orchestration",
                   version=request.profile_version, fields={
        code: ProfileField(value=value, status="confirmed", source_type="user_message")
        for code, value in facts.items()
        if code in clarify.field_definitions() and value is not None
    })


def _unsupported_question(task):
    aspects = {aspect for action in task.actions for aspect in action.aspects}
    return bool(aspects & {"fees", "timeline"}) and not bool(
        aspects & {"documents", "eligibility", "process"})


class VisaWorkflow:
    def __init__(self, *, knowledge=None, clock=None, generator=None):
        self.knowledge = knowledge
        self.clock = clock
        self.generator = generator or TemplateGenerator()

    async def run(self, task: DomainTask, request: OrchestrationInput) -> DomainResult:
        facts, basis = merged_facts(task, request)
        projection = "chat_only" if task.actions and all(
            action.operation in {"explain", "compare", "assess", "troubleshoot"}
            for action in task.actions) else "advice"
        if _unsupported_question(task):
            return DomainResult(
                task_id=task.task_id, domain="visa", status="needs_verification",
                answer=AnswerBody(
                    summary="当前已发布的签证知识只覆盖材料清单，不能可靠回答费用或办理时长。",
                    limitations=["请以目的地官方签证网站和实际受理机构的最新说明为准。"]),
                next_actions=["查看目的地官方签证网站的费用与处理时间"],
                evidence_refs=["visa-capability-boundary"], projection="chat_only",
                observed_facts=facts, observed_basis=basis,
                payload={"capability_boundary": "documents_only"}, model_version=MODEL_VERSION)

        profile = _profile(request, facts)
        assessment = clarify.assess("visa_documents", profile, facts,
                                    expected_profile_version=request.expected_profile_version)
        if not assessment.ready:
            return DomainResult(
                task_id=task.task_id, domain="visa", status="needs_input",
                answer=AnswerBody(summary=assessment.explanation,
                                  steps=["回答下方问题后，我再给出确定的材料清单。"]),
                missing_fields=assessment.missing_fields,
                follow_up_questions=assessment.questions, notices=assessment.notices,
                projection="chat_only", observed_facts=facts, observed_basis=basis,
                model_version=MODEL_VERSION)

        facts.update({code: fact.value for code, fact in assessment.facts.items()})
        if self.knowledge is None or self.clock is None:
            return DomainResult(
                task_id=task.task_id, domain="visa", status="cannot_answer",
                answer=AnswerBody(summary="签证知识读取服务尚未连接，暂时不能生成可靠材料清单。"),
                projection="chat_only", observed_facts=facts, observed_basis=basis,
                model_version=MODEL_VERSION)
        records = self.knowledge.search(
            country=facts["destination_country"], visa_category=facts["visa_category"],
            topic="documents", now=self.clock())
        scope = (f"{COUNTRY_ZH.get(facts['destination_country'], facts['destination_country'])} "
                 f"{VISA_ZH.get(facts['visa_category'], facts['visa_category'])}")
        if not records:
            return DomainResult(
                task_id=task.task_id, domain="visa", status="cannot_answer",
                answer=AnswerBody(summary=f"暂无{scope}的已发布材料资料，无法给出可靠清单。"),
                next_actions=_next_check(facts), projection="chat_only",
                observed_facts=facts, observed_basis=basis,
                knowledge_read_id=getattr(self.knowledge, "read_id", None), model_version=MODEL_VERSION)
        available = [record for record in records if record.available]
        if not available:
            reasons = sorted({record.availability_reason for record in records})
            return DomainResult(
                task_id=task.task_id, domain="visa", status="needs_verification",
                answer=AnswerBody(summary=f"{scope}的材料资料目前不可作为确定依据。",
                                  limitations=["资料状态：" + "、".join(reasons)]),
                notices=[Notice(code="knowledge_unavailable", message="、".join(reasons))],
                next_actions=_next_check(facts),
                evidence_refs=[f"{r.record.record_id}:{r.record.revision_id}" for r in records],
                projection="chat_only", observed_facts=facts, observed_basis=basis,
                knowledge_read_id=getattr(self.knowledge, "read_id", None), model_version=MODEL_VERSION)

        package = retrieve(records, facts)
        follow = clarify.assess("visa_documents", profile, facts,
                                condition_fields=package.condition_fields,
                                expected_profile_version=request.expected_profile_version)
        # Same UX as credit-card / legacy runner: finish condition questions
        # before emitting the materials list with many「待确认」rows.
        if follow.questions and not package.conflicts:
            notices = [*assessment.notices, *follow.notices,
                       Notice(code="pending_conditions", message=follow.explanation)]
            return DomainResult(
                task_id=task.task_id, domain="visa", status="needs_input",
                answer=AnswerBody(
                    summary=follow.explanation or "补充以下信息后，我再给出更准确的材料清单。",
                    steps=["回答下方问题后，我再给出确定的材料清单。"]),
                missing_fields=[],
                follow_up_questions=follow.questions,
                notices=notices,
                next_actions=_next_check(facts),
                projection="chat_only",
                observed_facts=facts, observed_basis=basis,
                knowledge_read_id=getattr(self.knowledge, "read_id", None),
                model_version=MODEL_VERSION)

        draft = self.generator.draft(facts=facts, package=package, assessment=follow)
        gated = visa_gate.run_gate(draft, package, self.knowledge, self.clock())
        citations = list(gated.citations)
        conflicts = list(package.conflicts) if gated.status == "needs_verification" else []
        if conflicts:
            known = {citation.citation_id for citation in citations}
            citations.extend(item.citation for item in package.items
                             if item.applicability == "conflict"
                             and item.citation.citation_id not in known)
        limitations = list(gated.limitations)
        if any(record.record.synthetic for record in available):
            limitations.insert(0, "【模拟资料】当前依据为合成演示资料，不能代替官方规定。")
        summary = (draft.summary if gated.status != "cannot_answer"
                   else "关键材料的引用未通过校验，无法给出可靠清单。")
        return DomainResult(
            task_id=task.task_id, domain="visa", status=gated.status,
            answer=AnswerBody(summary=summary,
                              steps=draft.steps if gated.status != "cannot_answer" else [],
                              materials=gated.materials if gated.status != "cannot_answer" else [],
                              limitations=limitations),
            claims=gated.claims if gated.status != "cannot_answer" else [],
            citations=citations if gated.status != "cannot_answer" else [], conflicts=conflicts,
            evidence_refs=[f"{r.record.record_id}:{r.record.revision_id}" for r in available],
            follow_up_questions=follow.questions, notices=[*assessment.notices, *follow.notices],
            next_actions=["提交前到目的地官方签证网站核对最新要求"], projection=projection,
            observed_facts=facts, observed_basis=basis,
            payload={"materials": [m.model_dump(mode="json") for m in gated.materials],
                     "citations": [c.model_dump(mode="json") for c in citations],
                     "unconfirmed_fields": follow.pending_fields},
            knowledge_read_id=getattr(self.knowledge, "read_id", None),
            model_version=getattr(self.generator, "model_version", MODEL_VERSION))
