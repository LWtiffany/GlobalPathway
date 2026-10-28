"""M05 编排：一次问答的显式状态机（英国 / 美国 / 香港学生签证材料）。

received → classified → (M06) needs_input | knowledge_loaded → retrieved → draft_ready → gated。
每个阶段单独计时（stage_ms），同一 trace_id 贯穿；规则阶段与模型阶段分开计时。
未经门控的草稿不会出现在结果里。
"""
import time
import uuid
from dataclasses import dataclass, field

from . import clarify, gate as gate_mod
from .generate import COUNTRY_ZH, PROMPT_VERSION, VISA_ZH, TemplateGenerator
from .intent import classify
from .models import AnswerBody, AnswerResult, Notice
from .retrieve import retrieve

MAX_MESSAGE_CHARS = 2000
TOPIC_ADVICE = {"visa_documents": "visa"}
KNOWLEDGE_TOPIC = {"visa_documents": "documents"}
UNIMPLEMENTED_TOPIC_SUMMARY = {
    "remittance_tuition": "学费汇款流程尚未接入，当前不能生成确定建议。",
    "remittance_living": "生活费汇款流程尚未接入，当前不能生成确定建议。",
    "overseas_spending": "境外消费与支付流程尚未接入，当前不能生成确定建议；这不表示所选地区不受支持。",
    "hsbc_account": "银行开户流程尚未接入，当前不能生成确定建议。",
    "credit_card": "信用卡流程应由 Agent2 v2 处理，本轮路由异常，未生成建议。",
}
OFFICIAL_ENTRY = {
    ("UK", "student_visa"): "GOV.UK Student visa 官方页面（https://www.gov.uk/student-visa）",
    ("US", "student_visa"): "美国国务院学生签证页面（https://travel.state.gov/content/travel/en/us-visas/study/student-visa.html）"
                            "及面签所在使领馆网站",
    ("CN-HK", "student_visa"): "香港入境事务处来港就读页面（https://www.immd.gov.hk/eng/services/visas/study.html）",
}
SCOPE_BOUNDARY = ("这个问题超出了留学准备助手的服务范围（选校/申请代办、租房合同审查、投资理财等不在范围内）。"
                  "我可以帮你看签证材料、学费/生活费汇款、汇丰开户和信用卡相关的准备事项。")


@dataclass
class RunOutput:
    result: AnswerResult
    turn_facts: dict
    turn_basis: dict
    topic: str | None
    knowledge_read_id: str | None = None
    model_version: str = "none"
    prompt_version: str = PROMPT_VERSION
    gate_version: str = gate_mod.GATE_VERSION
    advice: dict | None = None
    advice_items: list = field(default_factory=list)
    active_intents: list = field(default_factory=list)
    # Ordered assistant bubbles for one user turn (necessity + per-domain answers).
    # Empty means only `result` is persisted.
    message_parts: list = field(default_factory=list)
    stage_ms: dict = field(default_factory=dict)
    assessments: list = field(default_factory=list)


class _Timer:
    def __init__(self):
        self.stage_ms = {}
        self._last = time.perf_counter()

    def mark(self, stage):
        now = time.perf_counter()
        self.stage_ms[stage] = round((now - self._last) * 1000, 3)
        self._last = now

    def add(self, stage, ms):
        self.stage_ms[stage] = round(self.stage_ms.get(stage, 0) + ms, 3)


class Agent2Runner:
    def __init__(self, knowledge, clock, generator=None, suggest=None):
        self.knowledge = knowledge
        self.clock = clock
        self.generator = generator or TemplateGenerator()
        self.suggest = suggest

    def run(self, *, profile, text, conversation_facts=None, conversation_topic=None,
            asked_fields=(), expected_profile_version=None, message_id=None, trace_id=None):
        assessments = []
        output = self._run(profile, text, conversation_facts, conversation_topic, asked_fields,
                           expected_profile_version, message_id, trace_id, assessments)
        output.assessments = assessments
        return output

    def _run(self, profile, text, conversation_facts, conversation_topic, asked_fields,
             expected_profile_version, message_id, trace_id, assessments):
        timer = _Timer()
        trace_id = trace_id or str(uuid.uuid4())
        message_id = message_id or str(uuid.uuid4())

        def result(status, summary, **extra):
            return AnswerResult(message_id=message_id, status=status, profile_id=profile.profile_id,
                                profile_version=profile.version, trace_id=trace_id,
                                answer=AnswerBody(summary=summary, steps=extra.pop("steps", []),
                                                  materials=extra.pop("materials", []),
                                                  limitations=extra.pop("limitations", [])),
                                **extra)

        text = text if isinstance(text, str) else ""
        if not text.strip() or len(text) > MAX_MESSAGE_CHARS:
            reason = "消息为空" if not text.strip() else f"消息超过 {MAX_MESSAGE_CHARS} 字"
            out = result("needs_input", f"{reason}，请重新描述你的问题。",
                         notices=[Notice(code="invalid_input", message=reason)])
            timer.mark("validated")
            return self._finish(out, timer, {}, {}, None)

        turn = classify(text, asked_fields=asked_fields)
        topic = turn.intent
        if topic in ("follow_up", "other") and conversation_topic:
            topic = conversation_topic
        timer.mark("classified")
        if topic in ("out_of_scope", "other", "follow_up"):
            return self._finish(result("out_of_scope", SCOPE_BOUNDARY), timer, turn.facts, turn.basis, None)

        stated = {**(conversation_facts or {}), **turn.facts}
        assessment = clarify.assess(topic, profile, stated, expected_profile_version=expected_profile_version,
                                    suggest=self.suggest)
        assessments.append(assessment)
        timer.add("m06_rules", assessment.rule_ms)
        if assessment.model_ms:
            timer.add("m06_model", assessment.model_ms)
        notices = list(assessment.notices)
        if not assessment.ready:
            notices.append(Notice(code=assessment.state, message=assessment.explanation))
            out = result("needs_input", assessment.explanation,
                         steps=["回答下方问题后，我再给出确定的材料清单。"],
                         missing_fields=assessment.missing_fields,
                         follow_up_questions=assessment.questions, notices=notices)
            return self._finish(out, timer, turn.facts, turn.basis, topic)

        facts = {k: f.value for k, f in assessment.facts.items()}
        if topic not in KNOWLEDGE_TOPIC:
            out = result("cannot_answer", UNIMPLEMENTED_TOPIC_SUMMARY.get(
                topic, "该领域流程尚未接入，当前不能生成确定建议。"),
                         notices=notices)
            return self._finish(out, timer, turn.facts, turn.basis, topic)

        records = self.knowledge.search(country=facts["destination_country"],
                                        visa_category=facts["visa_category"],
                                        topic=KNOWLEDGE_TOPIC[topic], now=self.clock())
        timer.mark("knowledge_loaded")
        scope = f"{COUNTRY_ZH.get(facts['destination_country'], facts['destination_country'])} " \
                f"{VISA_ZH.get(facts['visa_category'], facts['visa_category'])}"
        official = OFFICIAL_ENTRY.get((facts["destination_country"], facts["visa_category"]))
        next_check = [f"到{official}核实最新要求。"] if official else ["到目的地官方签证网站核实最新要求。"]
        if not records:
            out = result("cannot_answer", f"暂无{scope}的已发布材料资料，无法给出可靠清单。",
                         notices=notices, next_actions=next_check)
            return self._finish(out, timer, turn.facts, turn.basis, topic, read_id=self.knowledge.read_id)
        if not any(r.available for r in records):
            reasons = sorted({r.availability_reason for r in records})
            out = result("needs_verification",
                         f"{scope}的材料资料目前不可作为确定依据（{', '.join(reasons)}），需核实后再用。",
                         notices=notices + [Notice(code="knowledge_unavailable", message=", ".join(reasons))],
                         next_actions=next_check)
            return self._finish(out, timer, turn.facts, turn.basis, topic, read_id=self.knowledge.read_id)

        package = retrieve(records, facts)
        timer.mark("retrieved")
        follow = clarify.assess(topic, profile, stated, condition_fields=package.condition_fields,
                                suggest=self.suggest)
        assessments.append(follow)
        timer.add("m06_rules", follow.rule_ms)
        if follow.model_ms:
            timer.add("m06_model", follow.model_ms)

        draft = self.generator.draft(facts=facts, package=package, assessment=follow)
        timer.mark("draft_ready")
        gated = gate_mod.run_gate(draft, package, self.knowledge, self.clock())
        timer.mark("gated")

        synthetic = any(r.record.synthetic for r in records if r.available)
        limitations = list(gated.limitations)
        if follow.assumption_gaps:
            limitations.append("未提供" + "、".join(clarify.label(f) for f in follow.assumption_gaps)
                               + "，本回答没有据此调整。")
        if synthetic:
            limitations.insert(0, "【模拟资料】本回答基于合成演示资料（synthetic），不是官方规定，不能作为真实签证依据。")
        citations = list(gated.citations)
        conflicts = list(package.conflicts) if gated.status == "needs_verification" else []
        if conflicts:
            known = {c.citation_id for c in citations}
            citations += [i.citation for i in package.items
                          if i.applicability == "conflict" and i.citation.citation_id not in known]
            notices.append(Notice(code="source_conflict", message="、".join(c.name_zh for c in conflicts)
                                  + " 在同范围资料间要求不一致，已列出全部来源，未任选其一。"))
        summary = draft.summary if gated.status != "cannot_answer" else \
            "关键材料的引用未通过校验，无法给出可靠清单。"
        out = result(gated.status, summary,
                     steps=draft.steps if gated.status != "cannot_answer" else [],
                     materials=gated.materials if gated.status != "cannot_answer" else [],
                     limitations=limitations, claims=gated.claims if gated.status != "cannot_answer" else [],
                     citations=citations if gated.status != "cannot_answer" else [],
                     conflicts=conflicts, follow_up_questions=follow.questions,
                     notices=notices + list(follow.notices),
                     next_actions=next_check, synthetic=synthetic)
        advice = None
        if out.status == "answered" and topic in TOPIC_ADVICE:
            advice = {"topic": TOPIC_ADVICE[topic], "payload": {
                "summary": out.answer.summary,
                "materials": [m.model_dump() for m in out.answer.materials],
                "citations": [c.model_dump(mode="json") for c in out.citations],
                "revisions": sorted({(c.record_id, c.revision_id) for c in out.citations}),
                "unconfirmed_fields": follow.pending_fields, "synthetic": synthetic}}
            out.advice_updates = [TOPIC_ADVICE[topic]]
        return self._finish(out, timer, turn.facts, turn.basis, topic, read_id=self.knowledge.read_id,
                            model_version=self.generator.model_version, advice=advice)

    @staticmethod
    def _finish(out, timer, facts, basis, topic, read_id=None, model_version="none", advice=None):
        out.stage_ms = dict(timer.stage_ms)
        return RunOutput(result=out, turn_facts=facts, turn_basis=basis, topic=topic,
                         knowledge_read_id=read_id, model_version=model_version, advice=advice,
                         stage_ms=out.stage_ms)
