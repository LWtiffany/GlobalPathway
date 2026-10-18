"""Run Agent2 credit-card follow-ups against the real configured LLM and RDS catalog.

Usage after turning the VPN off:
    python scripts/test_agent2_followups.py

The script uses a temporary local conversation store, but Profile orchestration, LLM calls,
and credit-card product reads use production code paths. It never prints secrets.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent2.knowledge import FixtureKnowledgeRead  # noqa: E402
from agent2.llm import LlmComponents, build_llm_components  # noqa: E402
from agent2.orchestration.domains.credit_card_repository import RdsCreditCardRepository  # noqa: E402
from agent2.service import Agent2Service  # noqa: E402
from agent2.store import Store  # noqa: E402

CREDIT_CARD_FIELDS = {
    "application_region", "applicant_age", "applicant_age_band",
    "annual_income_hkd", "annual_income_band_hkd", "asset_band",
    "repayment_mode", "spending_scenarios", "reward_preference",
    "full_time_student", "student_institution_eligible", "premier_relationship",
    "hsbc_relationship_balance_band",
    "hsbc_premier_path", "hk_resident",
    "monthly_card_spend_band_hkd",
    "mainland_payment_method",
}


@dataclass
class TurnRecord:
    turn: int
    input_mode: str
    user_text: str
    elapsed_seconds: float
    status: str
    summary: str
    steps: list[str]
    asked_fields: list[str]
    missing_fields: list[str]
    proposal_fields: list[str]
    profile_version: int
    model_version: str
    prompt_version: str
    knowledge_read_id: str | None
    understanding_llm_called: bool
    writer_llm_called: bool
    fallback: bool
    stage_ms: dict = field(default_factory=dict)
    llm_calls: list[dict] = field(default_factory=list)
    recommendations: dict = field(default_factory=dict)
    catalog: dict = field(default_factory=dict)


@dataclass
class ScenarioReport:
    name: str
    ok: bool
    checks: dict[str, bool]
    turns: list[TurnRecord]
    final_profile: dict
    seed_profile_fields: list[str] = field(default_factory=list)
    final_recommendations: dict = field(default_factory=dict)


@dataclass
class LlmCallRecord:
    operation: str
    elapsed_seconds: float
    ok: bool
    error_type: str | None = None
    error_message: str | None = None


def safe_error_message(exc):
    message = str(exc).replace("\r", " ").replace("\n", " ")
    message = re.sub(r"(?i)bearer\s+\S+", "Bearer [REDACTED]", message)
    message = re.sub(
        r"(?i)(api[_ -]?key|authorization)(\s*[:=]?\s*)\S+",
        r"\1\2[REDACTED]",
        message,
    )
    return message[:500]


class RecordingUnderstanding:
    def __init__(self, delegate, records):
        self.delegate = delegate
        self.records = records
        self.model_version = delegate.model_version
        self.prompt_version = delegate.prompt_version

    async def understand(self, request, allowed_fields):
        started = time.perf_counter()
        try:
            result = await self.delegate.understand(request, allowed_fields)
        except Exception as exc:
            self.records.append(LlmCallRecord(
                operation="understanding",
                elapsed_seconds=round(time.perf_counter() - started, 3),
                ok=False,
                error_type=type(exc).__name__,
                error_message=safe_error_message(exc),
            ))
            raise
        self.records.append(LlmCallRecord(
            operation="understanding",
            elapsed_seconds=round(time.perf_counter() - started, 3),
            ok=True,
        ))
        return result


class RecordingWriter:
    def __init__(self, delegate, records):
        self.delegate = delegate
        self.records = records
        self.model_version = delegate.model_version
        self.prompt_version = delegate.prompt_version

    async def write(self, *, request, verified_answer):
        started = time.perf_counter()
        try:
            result = await self.delegate.write(request=request, verified_answer=verified_answer)
        except Exception as exc:
            self.records.append(LlmCallRecord(
                operation="answer_writer",
                elapsed_seconds=round(time.perf_counter() - started, 3),
                ok=False,
                error_type=type(exc).__name__,
                error_message=safe_error_message(exc),
            ))
            raise
        self.records.append(LlmCallRecord(
            operation="answer_writer",
            elapsed_seconds=round(time.perf_counter() - started, 3),
            ok=True,
        ))
        return result


class RecordingWorkflow:
    """Observe real orchestration outputs without replacing any production dependency."""

    def __init__(self, delegate, outputs):
        self.delegate = delegate
        self.outputs = outputs

    def run(self, request):
        output = self.delegate.run(request)
        self.outputs.append(output)
        return output


def load_profile_fixture(path):
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(payload, dict) and "fields" in payload:
        payload = payload["fields"]
    if not isinstance(payload, dict):
        raise ValueError("Profile fixture 必须是字段到值的 JSON 对象")
    return payload


def new_service(scenario, profile_values=None):
    store = Store()
    llm_calls = []
    workflow_outputs = []
    components = build_llm_components()
    recorded_components = LlmComponents(
        understanding=RecordingUnderstanding(components.understanding, llm_calls)
        if components.understanding else None,
        writer=RecordingWriter(components.writer, llm_calls) if components.writer else None,
    )
    service = Agent2Service(
        store,
        FixtureKnowledgeRead.from_file(),
        credit_card_repository=RdsCreditCardRepository(),
        llm_components=recorded_components,
    )
    service.v2_workflow = RecordingWorkflow(service.v2_workflow, workflow_outputs)
    user_id = service.demo_login(f"followup-{scenario}")["user_id"]
    profile = service.create_profile(user_id, fields=profile_values or None, synthetic=True)
    session_id = service.create_conversation(user_id, profile.profile_id)["conversation_id"]
    return store, service, user_id, profile.profile_id, session_id, llm_calls, workflow_outputs


def answer_run(service, message_id):
    return service.store.one(
        "SELECT model_version,prompt_version,knowledge_read_id "
        "FROM answer_runs WHERE message_id=?", (message_id,))


def send_turn(service, user_id, profile_id, session_id, turn, text, *, confirmed_fields=None,
              llm_calls=None, workflow_outputs=None):
    profile_before = service.get_profile(user_id, profile_id)
    call_offset = len(llm_calls or [])
    workflow_offset = len(workflow_outputs or [])
    started = time.perf_counter()
    answer = service.post_message(
        user_id, session_id, text, f"turn-{turn}",
        confirmed_fields=confirmed_fields,
        expected_profile_version=profile_before.version if confirmed_fields else None,
    )
    elapsed = round(time.perf_counter() - started, 3)
    profile_after = service.get_profile(user_id, profile_id)
    run = answer_run(service, answer.message_id)
    prompt_version = run["prompt_version"]
    proposals = service.list_proposals(user_id, profile_id)
    turn_outputs = (workflow_outputs or [])[workflow_offset:]
    recommendations = {}
    catalog = {}
    if turn_outputs and turn_outputs[-1].answer.domain_results:
        payload = turn_outputs[-1].answer.domain_results[0].payload
        recommendations = payload.get("recommendations", {})
        catalog = payload.get("catalog", {})
    return answer, TurnRecord(
        turn=turn,
        input_mode="structured_selection" if confirmed_fields else "free_text",
        user_text=text,
        elapsed_seconds=elapsed,
        status=answer.status,
        summary=answer.answer.summary,
        steps=answer.answer.steps,
        asked_fields=[item.field for item in answer.follow_up_questions],
        missing_fields=answer.missing_fields,
        proposal_fields=sorted({item.field_code for item in proposals}),
        profile_version=profile_after.version,
        model_version=run["model_version"],
        prompt_version=prompt_version,
        knowledge_read_id=run["knowledge_read_id"],
        understanding_llm_called="understand-user-v1" in prompt_version,
        writer_llm_called="write-answer-v1" in prompt_version,
        fallback=any(item.code == "llm_fallback" for item in answer.notices),
        stage_ms=answer.stage_ms,
        llm_calls=[asdict(item) for item in (llm_calls or [])[call_offset:]],
        recommendations=recommendations,
        catalog=catalog,
    )


def chosen_value(question):
    preferred = {
        "application_region": "CN-HK",
        "applicant_age_band": "18_or_over",
        "hk_resident": True,
        "hsbc_relationship_balance_band": "1m_plus",
        "hsbc_premier_path": "not_applicable",
        "asset_band": "under_100k",
        "student_institution_eligible": True,
        "repayment_mode": "full",
        "spending_scenarios": ["online", "dining"],
        "monthly_card_spend_band_hkd": "5k_10k",
        "reward_preference": "no_preference",
    }
    if question.field in preferred:
        return preferred[question.field]
    if question.field == "annual_income_band_hkd":
        return question.options[-1]["value"]
    raise RuntimeError(f"测试脚本未配置字段 {question.field} 的选择值")


def run_structured_choices(profile_values=None):
    store, service, user_id, profile_id, session_id, llm_calls, workflow_outputs = new_service(
        "structured", profile_values)
    turns = []
    try:
        answer, record = send_turn(service, user_id, profile_id, session_id, 1, "我想申请信用卡",
                                   llm_calls=llm_calls, workflow_outputs=workflow_outputs)
        turns.append(record)
        turn = 2
        # Eligibility, Premier and conditional reward questions can take seven
        # structured turns; keep the automated path aligned with interactive mode.
        while answer.status == "needs_input" and turn <= 8:
            selected = {question.field: chosen_value(question)
                        for question in answer.follow_up_questions}
            labels = []
            for question in answer.follow_up_questions:
                value = selected[question.field]
                values = value if isinstance(value, list) else [value]
                labels.extend(option["label"] for option in question.options
                              if option["value"] in values)
            answer, record = send_turn(
                service, user_id, profile_id, session_id, turn,
                "；".join(labels), confirmed_fields=selected, llm_calls=llm_calls,
                workflow_outputs=workflow_outputs)
            turns.append(record)
            turn += 1
        asked = [field for item in turns for field in item.asked_fields]
        final = turns[-1]
        structured_turns = turns[1:]
        checks = {
            "final_recommendation_ready": final.status in {"answered", "needs_verification"}
                                          and bool(final.recommendations.get("primary")),
            "initial_understanding_called": turns[0].understanding_llm_called,
            "structured_turns_skip_understanding": all(not item.understanding_llm_called
                                                       for item in structured_turns),
            "writer_only_on_final": all(not item.writer_llm_called for item in turns[:-1])
                                    and final.writer_llm_called,
            "no_repeated_questions": len(asked) == len(set(asked)),
            "max_two_questions_per_turn": all(len(item.asked_fields) <= 2 for item in turns),
            "profile_updated": final.profile_version > 1,
            "no_open_proposals": not final.proposal_fields,
            "no_llm_fallback": not any(item.fallback for item in turns),
            "eligibility_explained": any("资格：" in step for step in final.steps),
            "rds_catalog_used": any(
                (item.knowledge_read_id or "").startswith("rds-credit-card-") for item in turns),
            "no_cross_domain_questions": all(
                set(item.asked_fields) <= CREDIT_CARD_FIELDS for item in turns),
            "catalog_inventory_complete": any(
                item.catalog.get("returned_products") == item.catalog.get("source_active")
                for item in turns if item.catalog),
        }
        profile = service.get_profile(user_id, profile_id).model_dump(mode="json")
        recommendations = next((item.recommendations for item in reversed(turns)
                                if item.recommendations), {})
        return ScenarioReport("structured_choice_followups", all(checks.values()), checks, turns, profile,
                              sorted(profile_values or {}), recommendations)
    finally:
        store.close()


def run_interactive(profile_values=None):
    store, service, user_id, profile_id, session_id, llm_calls, workflow_outputs = new_service(
        "interactive", profile_values)
    turns = []
    try:
        initial = input("请输入初始问题（直接回车使用：我想申请信用卡）：").strip()
        initial = initial or "我想申请信用卡"
        answer, record = send_turn(service, user_id, profile_id, session_id, 1, initial,
                                   llm_calls=llm_calls, workflow_outputs=workflow_outputs)
        turns.append(record)
        turn = 2
        while answer.status == "needs_input" and turn <= 8:
            print(f"\n第 {turn - 1} 轮追问：")
            selected = {}
            labels = []
            for question in answer.follow_up_questions:
                print(f"\n{question.text}")
                for index, option in enumerate(question.options, 1):
                    print(f"  {index}. {option['label']}")
                while True:
                    raw = input("请输入选项编号（多选用逗号分隔）：").strip()
                    try:
                        indexes = [int(item.strip()) for item in raw.split(",") if item.strip()]
                        if question.input_type != "multi_select" and len(indexes) != 1:
                            raise ValueError
                        if not indexes or len(indexes) != len(set(indexes)):
                            raise ValueError
                        if any(index < 1 or index > len(question.options) for index in indexes):
                            raise ValueError
                        break
                    except ValueError:
                        print("输入无效，请输入列表中的编号。")
                values = [question.options[index - 1]["value"] for index in indexes]
                selected[question.field] = values if question.input_type == "multi_select" else values[0]
                labels.extend(question.options[index - 1]["label"] for index in indexes)
            answer, record = send_turn(
                service, user_id, profile_id, session_id, turn,
                "；".join(labels), confirmed_fields=selected, llm_calls=llm_calls,
                workflow_outputs=workflow_outputs)
            turns.append(record)
            print(f"\n本轮状态：{answer.status}")
            print(f"回答：{answer.answer.summary}")
            turn += 1
        asked = [field for item in turns for field in item.asked_fields]
        final = turns[-1]
        checks = {
            "final_recommendation_ready": final.status in {"answered", "needs_verification"}
                                          and bool(final.recommendations.get("primary")),
            "initial_understanding_called": turns[0].understanding_llm_called,
            "followup_understanding_skipped": all(not item.understanding_llm_called for item in turns[1:]),
            "writer_only_on_final": all(not item.writer_llm_called for item in turns[:-1])
                                    and final.writer_llm_called,
            "no_repeated_questions": len(asked) == len(set(asked)),
            "max_two_questions_per_turn": all(len(item.asked_fields) <= 2 for item in turns),
            "profile_updated": final.profile_version > 1,
            "no_open_proposals": not final.proposal_fields,
            "no_llm_fallback": not any(item.fallback for item in turns),
            "eligibility_explained": any("资格：" in step for step in final.steps),
            "rds_catalog_used": any(
                (item.knowledge_read_id or "").startswith("rds-credit-card-") for item in turns),
            "no_cross_domain_questions": all(
                set(item.asked_fields) <= CREDIT_CARD_FIELDS for item in turns),
            "catalog_inventory_complete": any(
                item.catalog.get("returned_products") == item.catalog.get("source_active")
                for item in turns if item.catalog),
        }
        profile = service.get_profile(user_id, profile_id).model_dump(mode="json")
        recommendations = next((item.recommendations for item in reversed(turns)
                                if item.recommendations), {})
        return ScenarioReport("interactive_structured_followups", all(checks.values()), checks, turns, profile,
                              sorted(profile_values or {}), recommendations)
    finally:
        store.close()


def print_scenario(report):
    print(f"\n=== {report.name} ===")
    for turn in report.turns:
        calls = []
        if turn.understanding_llm_called:
            calls.append("理解LLM")
        if turn.writer_llm_called:
            calls.append("写作LLM")
        print(f"第 {turn.turn} 轮 [{turn.input_mode}] {turn.elapsed_seconds:.3f} 秒")
        print(f"  用户：{turn.user_text}")
        print(f"  状态：{turn.status}；LLM：{'、'.join(calls) or '无'}")
        print(f"  产品读取：{turn.knowledge_read_id or '本轮未读取'}")
        print(f"  追问：{', '.join(turn.asked_fields) or '无'}")
        print(f"  回答：{turn.summary}")
        for step in turn.steps:
            print(f"    - {step}")
        for call in turn.llm_calls:
            if call["ok"]:
                print(f"  LLM诊断：{call['operation']} 成功，{call['elapsed_seconds']:.3f} 秒")
            else:
                print(f"  LLM诊断：{call['operation']} 失败，{call['elapsed_seconds']:.3f} 秒，"
                      f"{call['error_type']}: {call['error_message']}")
    for name, ok in report.checks.items():
        print(f"  {'通过' if ok else '失败'}：{name}")
    if report.final_recommendations:
        catalog = next((turn.catalog for turn in reversed(report.turns) if turn.catalog), {})
        if catalog:
            print("  RDS 产品库："
                  f"总记录 {catalog.get('source_total')}，ACTIVE {catalog.get('source_active')}，"
                  f"本次返回 {catalog.get('returned_products')}，过滤 {catalog.get('source_filtered')}")
        ranked = report.final_recommendations.get("ranked_candidates") or [
            report.final_recommendations.get("primary"),
            *report.final_recommendations.get("supplements", []),
            *report.final_recommendations.get("alternatives", []),
        ]
        primary_id = (report.final_recommendations.get("primary") or {}).get("card_id")
        print("  RDS 完整候选排名与评分：")
        for index, card in enumerate((item for item in ranked if item), 1):
            marker = "主推荐" if card["card_id"] == primary_id else f"第 {index} 名"
            breakdown = "，".join(f"{name}={value}"
                                   for name, value in card.get("score_breakdown", {}).items())
            print(f"    - {marker}: {card['product_name']}，annual_net_hkd={card['score']}，"
                  f"eligibility={card['eligibility']['status']}；{breakdown}")
            if card["eligibility"].get("missing_fields"):
                print("      待确认资格字段：" + ", ".join(card["eligibility"]["missing_fields"]))
        for card_id, reasons in report.final_recommendations.get("excluded", {}).items():
            print(f"    - 已排除: {card_id}；{'；'.join(reasons)}")
    print(f"场景结果：{'通过' if report.ok else '失败'}")


def main(argv=None):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="自动测试 Agent2 用户追问流程")
    parser.add_argument("--output", help="JSON 报告输出路径")
    parser.add_argument("--auto", action="store_true", help="跳过人工输入，自动回放选择题")
    parser.add_argument("--profile-file", help="预先写入 Profile 的 JSON 文件（字段代码到值）")
    args = parser.parse_args(argv)
    started = time.perf_counter()
    scenarios = []
    fatal_error = None
    try:
        profile_values = load_profile_fixture(args.profile_file) if args.profile_file else {}
        scenarios.append(run_structured_choices(profile_values) if args.auto else run_interactive(profile_values))
    except Exception as exc:
        fatal_error = {"type": type(exc).__name__, "message": str(exc)[:500]}
        print(f"测试中止：{fatal_error['type']}：{fatal_error['message']}")
    for scenario in scenarios:
        print_scenario(scenario)
    created_at = datetime.now(timezone.utc)
    report = {
        "created_at": created_at.isoformat(),
        "network_note": "VPN off",
        "total_seconds": round(time.perf_counter() - started, 3),
        "ok": fatal_error is None and len(scenarios) == 1 and all(item.ok for item in scenarios),
        "fatal_error": fatal_error,
        "scenarios": [asdict(item) for item in scenarios],
    }
    output = Path(args.output) if args.output else \
        ROOT / "data" / "agent2" / "eval" / f"followup-test-{created_at:%Y%m%dT%H%M%SZ}.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n总结果：{'通过' if report['ok'] else '失败'}，耗时 {report['total_seconds']:.3f} 秒")
    print(f"报告已保存：{output}")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
