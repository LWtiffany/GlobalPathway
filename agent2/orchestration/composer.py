"""Deterministic response composition with a single-intent fast path."""

import re

from agent2.intent import is_investment_account
from agent2.models import AnswerBody, FollowUpQuestion, Notice
from agent2.runner import SCOPE_BOUNDARY

from .contracts import JourneyStep, QualityReport, TaskPlan, UnifiedAnswer

DOMAIN_LABEL = {"visa": "签证", "fx": "外汇与汇款", "bank_account": "银行开户", "credit_card": "信用卡"}
MATERIAL_TAG = {"required": "必需", "conditional": "视情况", "unverified": "待核实"}
# Multi-intent turns: at most one follow-up per domain, hard cap across domains.
MAX_FOLLOWUPS_MULTI = 2

REFUSAL_OTHER = (
    "暂时无法识别为签证、外汇汇款、银行开户或信用卡问题。"
    "如果想办理日常收付款账户，可以说「英国留学怎么开户」或「香港储蓄卡要什么材料」；"
    "如果想比较信用卡，可以说「推荐信用卡」。"
)
CLARIFY_BANK_CARD = (
    "当然可以。为了不把银行账户／借记卡和信用卡混在一起，请您先选一下"
    "想了解的方向，我会接着为您整理。"
)
PARTIAL_OOS = "部分内容超出服务范围（选校/代办/租房合同/投资理财等），未纳入本轮建议。"
INVESTMENT_ACCOUNT_SCOPE = (
    "您问的是股票／证券交易账户，不是日常收付款使用的银行往来或储蓄账户。"
    "目前这里的开户资料只覆盖汇丰银行账户和借记卡，不能用 HSBC Bank Account 的条件代替证券账户条件。"
)
# Routing audit stays on TaskPlan.routing_audit / answer snapshot — never in user notices.

# 「需不需要 / 要不要 / …吗」先给结论，再展开产品建议。
# Materials asks like「是否需要准备什么材料」must NOT match（含「是否需要」子串）。
_NEED_QUESTION = re.compile(
    r"需不需要|要不要|有没有必要|是否需要|有必要(?:吗|么)?"
    r"|我?需要(?:开|办).{0,20}吗"
    r"|(?:要(?:开|办)|开|办)(?:银行账户|银行开户|开户|信用卡|汇丰卡).{0,6}吗",
    re.I,
)
_MATERIALS_ASK = re.compile(
    r"(?:准备|需要).{0,12}(?:什么|哪些)?(?:材料|文件|证件|清单)"
    r"|(?:材料|文件|证件|清单).{0,8}(?:准备|需要)",
    re.I,
)

_NECESSITY_VERDICT = {
    "bank_account": (
        "通常很实用。当地银行账户方便收付款、交房租和生活费；"
        "部分当地信用卡也会要求先有合资格的银行关系。"
    ),
    "credit_card": (
        "不是硬性必需，但很实用。订票、酒店和日常消费更方便；"
        "有当地银行账户后再申请通常更顺。"
    ),
    "fx": (
        "若学费或生活费要从境内汇出，一般需要按购汇/汇款要求办理；"
        "有学校与项目后可对照目录学费与额度，再按银行材料要求汇出。"
    ),
    "visa": (
        "留学通常需要办理相应学生签证；是否已持有有效签证以您的行程和官方要求为准。"
    ),
}

_JOURNEY_ORDER = {"visa": 10, "bank_account": 20, "fx": 30, "credit_card": 40}
_JOURNEY_GUIDANCE = {
    "visa": "先确认签证类别与材料；它会影响后续时间安排。",
    "bank_account": "确认可开户地区与账户条件，为收付款和后续办卡做准备。",
    "fx": "核对学校缴费通知、目录学费、购汇额度与材料；可与签证、开户准备并行。",
    "credit_card": "在申请地区、收入与银行关系明确后再选卡，减少无效申请。",
}


def compose_journey(plan: TaskPlan, report: QualityReport) -> list[JourneyStep]:
    """Build guidance without turning recommendations into hard dependencies."""
    if len(plan.tasks) < 3:
        return []
    result_by_task = {item.task_id: item for item in report.results}
    tasks = sorted(plan.tasks, key=lambda item: (_JOURNEY_ORDER[item.domain], item.task_id))
    journey = []
    for index, task in enumerate(tasks, 1):
        result = result_by_task.get(task.task_id)
        suffix = "" if result is None or result.status == "answered" else "（本轮仍需补充或核实）"
        journey.append(JourneyStep(
            order=index, task_id=task.task_id, domain=task.domain,
            label=DOMAIN_LABEL[task.domain],
            guidance=_JOURNEY_GUIDANCE[task.domain] + suffix,
            can_run_in_parallel=task.domain in {"visa", "bank_account", "fx"},
        ))
    return journey


def is_necessity_question(text: str) -> bool:
    if not text or _MATERIALS_ASK.search(text):
        return False
    return bool(_NEED_QUESTION.search(text))


def necessity_summary(domains: list[str]) -> str:
    labels = [DOMAIN_LABEL[d] for d in domains if d in DOMAIN_LABEL]
    if domains == ["bank_account", "credit_card"] or set(domains) == {"bank_account", "credit_card"}:
        return ("当地账户通常很实用；信用卡不是硬性必需。"
                "下面先说结论；如果您想办卡，我再为您比较合适的方案。")
    if len(labels) == 1:
        short = {
            "bank_account": "通常很实用。下面为您说明怎么开、需要准备什么。",
            "credit_card": "不是硬性必需。若您想办，我可以继续帮您比较卡片。",
            "fx": "涉及跨境付学费/生活费时通常需要。下面是具体准备。",
            "visa": "通常需要。下面是材料与要点。",
        }.get(domains[0], "结论如下，随后是具体建议。")
        return short
    if len(labels) == 2:
        return f"关于{labels[0]}和{labels[1]}：先回答要不要，再分开给具体建议。"
    return f"关于{'、'.join(labels[:-1])}和{labels[-1]}：先回答要不要，再分开给具体建议。"


def necessity_preamble(domains: list[str]) -> list[str]:
    lines = ["## 先说要不要"]
    for domain in domains:
        verdict = _NECESSITY_VERDICT.get(domain)
        if verdict:
            lines.append(f"{DOMAIN_LABEL[domain]}：{verdict}")
    return lines


# Back-compat aliases used by older call sites / tests.
_necessity_summary = necessity_summary
_necessity_preamble = necessity_preamble


class DefaultResponseComposer:
    async def compose(self, plan: TaskPlan, report: QualityReport,
                      *, user_text: str = "") -> UnifiedAnswer:
        if not report.results:
            reason = (plan.routing_audit.refusal_reason if plan.routing_audit
                      else (plan.unsupported_intents[0] if plan.unsupported_intents else "other"))
            if reason == "ambiguous_card":
                original = user_text.strip()
                account_message = "我想了解银行账户或借记卡开户"
                card_message = "我想了解信用卡申请和推荐"
                if original:
                    account_message += f"。接着刚才的问题：{original}"
                    card_message += f"。接着刚才的问题：{original}"
                return UnifiedAnswer(
                    status="needs_input",
                    answer=AnswerBody(summary=CLARIFY_BANK_CARD),
                    missing_fields=["banking_help_type"],
                    follow_up_questions=[FollowUpQuestion(
                        question_code="choose_banking_help_type",
                        field="banking_help_type",
                        text="没问题，我先确认一下：您这次更想了解哪一类？",
                        rule="ambiguous_bank_card_v2",
                        profile_version=0,
                        input_type="single_select",
                        options=[
                            {"value": "bank_account", "label": "开银行账户／办借记卡",
                             "message": account_message},
                            {"value": "credit_card", "label": "申请／挑选信用卡",
                             "message": card_message},
                        ],
                        persist_mode="session_only",
                    )],
                )
            if reason == "out_of_scope" or "out_of_scope" in plan.unsupported_intents:
                summary = INVESTMENT_ACCOUNT_SCOPE if is_investment_account(user_text) else SCOPE_BOUNDARY
            else:
                summary = REFUSAL_OTHER
            return UnifiedAnswer(
                status="out_of_scope",
                answer=AnswerBody(summary=summary),
            )

        ask_need = is_necessity_question(user_text)
        domains = [item.domain for item in report.results]
        journey = compose_journey(plan, report)

        if len(report.results) == 1 and not plan.unsupported_intents:
            result = report.results[0]
            # A workflow may answer from currently known facts and offer optional
            # questions that further narrow conditional items.  The presence of
            # such questions alone must not turn an answered result into a block.
            effective_status = result.status
            limitations = list(result.answer.limitations)
            if plan.tasks and plan.tasks[0].source == "llm":
                limitations.append(
                    f"若您问的不是{DOMAIN_LABEL[plan.tasks[0].domain]}，请换个更明确的说法。"
                )
            answer = result.answer if not limitations else result.answer.model_copy(
                update={"limitations": limitations})
            # Only wrap finished answers. Asking follow-ups under a「先说要不要」
            # shell makes materials clarification look like a nonsense verdict.
            if (ask_need and result.status in {"answered", "needs_verification"}
                    and not result.follow_up_questions):
                # Single-bubble path keeps necessity inline; multi-intent chat
                # splits into separate messages in the legacy adapter instead.
                preamble = necessity_preamble(domains)
                steps = list(preamble)
                if answer.summary:
                    steps.append(answer.summary)
                steps.extend(s for s in answer.steps if s and s != answer.summary)
                answer = answer.model_copy(update={
                    "summary": necessity_summary(domains),
                    "steps": steps,
                })
            return UnifiedAnswer(
                status=effective_status, answer=answer, domain_results=report.results,
                missing_fields=result.missing_fields, next_actions=result.next_actions,
                notices=result.notices, follow_up_questions=result.follow_up_questions,
            )

        statuses = [item.status for item in report.results]
        if "needs_input" in statuses:
            status = "needs_input"
        elif all(item == "answered" for item in statuses):
            status = "answered"
        elif all(item == "cannot_answer" for item in statuses):
            status = "cannot_answer"
        else:
            status = "needs_verification"

        # Each domain already ran its full workflow; merge complete answers without
        # truncating steps/materials (side-panel advice uses the same DomainResult).
        sections: list[str] = []
        labels = []
        pending_followups = any(item.follow_up_questions for item in report.results)
        show_need = (
            ask_need and status in {"answered", "needs_verification"} and not pending_followups)
        if show_need:
            sections.extend(necessity_preamble(domains))
        if journey:
            sections.append("## 建议推进顺序")
            sections.extend(
                f"{step.order}. {step.label}：{step.guidance}" for step in journey)
        for item in report.results:
            label = DOMAIN_LABEL[item.domain]
            labels.append(label)
            sections.append(f"## {label}")
            if item.answer.summary:
                sections.append(item.answer.summary)
            for step in item.answer.steps:
                if step and step != item.answer.summary:
                    sections.append(step)
            for material in item.answer.materials:
                tag = MATERIAL_TAG.get(material.applicability, "")
                line = f"{material.name_zh}（{tag}）" if tag else material.name_zh
                if material.name_en and material.name_en != material.name_zh:
                    line += f" / {material.name_en}"
                if material.condition_text:
                    line += f"；条件：{material.condition_text}"
                sections.append(line)

        if show_need:
            summary = necessity_summary(domains)
        elif len(labels) == 2:
            summary = f"{labels[0]}和{labels[1]}我分开说。"
        elif len(labels) > 2:
            summary = f"{'、'.join(labels[:-1])}和{labels[-1]}我分开说。"
        else:
            summary = labels[0] if labels else ""

        limitations = [note for item in report.results for note in item.answer.limitations]
        notices = [note for item in report.results for note in item.notices]
        # Keep materials on the unified body for citation/todo wiring; chat UI nests
        # them under ## sections for multi-intent and skips the top-level list.
        materials = [m for item in report.results for m in item.answer.materials]
        if plan.unsupported_intents:
            limitations.append(PARTIAL_OOS)

        follow_ups = []
        seen_fields: set[str] = set()
        for item in report.results:
            if len(follow_ups) >= MAX_FOLLOWUPS_MULTI:
                break
            for question in item.follow_up_questions:
                if question.field in seen_fields:
                    continue
                follow_ups.append(question)
                seen_fields.add(question.field)
                break  # one follow-up per domain

        if plan.tasks and all(task.source == "llm" for task in plan.tasks):
            joined = "、".join(DOMAIN_LABEL[task.domain] for task in plan.tasks)
            limitations.append(f"若您问的不是{joined}，请换个更明确的说法。")

        return UnifiedAnswer(
            status=status,
            answer=AnswerBody(summary=summary, steps=sections,
                              materials=materials, limitations=limitations),
            domain_results=report.results,
            missing_fields=list(dict.fromkeys(f for item in report.results for f in item.missing_fields)),
            next_actions=list(dict.fromkeys(a for item in report.results for a in item.next_actions)),
            notices=notices,
            follow_up_questions=follow_ups,
            journey=journey,
        )
