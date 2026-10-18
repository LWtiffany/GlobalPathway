"""Deterministic response composition with a single-intent fast path."""

from agent2.models import AnswerBody, Notice

from .contracts import QualityReport, TaskPlan, UnifiedAnswer

DOMAIN_LABEL = {"visa": "签证", "fx": "外汇与汇款", "bank_account": "银行开户", "credit_card": "信用卡"}


class DefaultResponseComposer:
    async def compose(self, plan: TaskPlan, report: QualityReport) -> UnifiedAnswer:
        if not report.results:
            return UnifiedAnswer(
                status="out_of_scope",
                answer=AnswerBody(summary="暂时无法识别为签证、外汇汇款、银行开户或信用卡问题。"),
                notices=[Notice(code="unsupported_intent", message=", ".join(plan.unsupported_intents))],
            )
        if len(report.results) == 1 and not plan.unsupported_intents:
            result = report.results[0]
            return UnifiedAnswer(status=result.status, answer=result.answer, domain_results=report.results,
                                 missing_fields=result.missing_fields, next_actions=result.next_actions,
                                 notices=result.notices, follow_up_questions=result.follow_up_questions)

        statuses = [item.status for item in report.results]
        if "needs_input" in statuses:
            status = "needs_input"
        elif all(item == "answered" for item in statuses):
            status = "answered"
        elif all(item == "cannot_answer" for item in statuses):
            status = "cannot_answer"
        else:
            status = "needs_verification"
        sections = [f"【{DOMAIN_LABEL[item.domain]}】{item.answer.summary}" for item in report.results]
        limitations = [note for item in report.results for note in item.answer.limitations]
        if plan.unsupported_intents:
            limitations.append("部分问题超出当前四个领域的范围，未纳入建议。")
        return UnifiedAnswer(
            status=status,
            answer=AnswerBody(summary="已按领域拆分并汇总。", steps=sections, limitations=limitations),
            domain_results=report.results,
            missing_fields=list(dict.fromkeys(f for item in report.results for f in item.missing_fields)),
            next_actions=list(dict.fromkeys(a for item in report.results for a in item.next_actions)),
            notices=[note for item in report.results for note in item.notices],
            follow_up_questions=[question for item in report.results
                                 for question in item.follow_up_questions][:2],
        )
