"""M06 缺失条件判断与追问的分模块测试指标（docs/agent2-m06-metrics.md，对应设计 11.3.1）。

在固定回归集上跑真实的 M05→M06→M07/M08 流程，逐条记录输入夹具版本、预期、实际、阶段耗时和
trace_id，并汇总：
- 关键缺失条件识别召回率 = 识别出的必需缺失字段数 / 人工标注的必需缺失字段数
- 额外追问率 = 不影响本轮结论却被要求补充的字段数 / 全部追问字段数
- 每轮问题数超过 2、已确认字段被重复追问、未知值被猜作已确认、国别或签证类别未知却输出确定
  材料清单、异常/冲突/版本变化未给出可解释状态：均计为错误（发布门槛 0 次）
- 规则判定阶段 P95（模型阶段单独计时）
"""
import json
import math
import time
import uuid
from datetime import datetime
from pathlib import Path

from .clarify import MAX_QUESTIONS_PER_ROUND
from .knowledge import FIXTURES, FixtureKnowledgeRead
from .models import Profile, ProfileField
from .runner import Agent2Runner

DEFAULT_CASES = FIXTURES / "m06_cases.json"
TARGETS = {
    "s1_recall": 1.0,             # S1 英国 Student visa 固定案例 100%
    "extra_question_rate": 0.10,  # 试点建议 ≤10%
    "rule_p95_ms": 100.0,         # 规则判定阶段 P95 建议 100 毫秒
}
ERROR_KINDS = ("too_many_questions", "repeated_ask", "guessed_unknown", "definitive_list_without_must_have",
               "unexplained_state", "state_mismatch", "status_mismatch")


def _profile(case):
    spec = case.get("profile") or {}
    fields = {code: ProfileField(value=f.get("value"), status=f.get("status", "unknown"),
                                 source="user" if f.get("status") == "confirmed" else None)
              for code, f in (spec.get("fields") or {}).items()}
    return Profile(profile_id=f"eval-{case['case_id']}", user_id="eval", version=spec.get("version", 1),
                   synthetic=True, fields=fields)


def _p95(values):
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[max(0, math.ceil(0.95 * len(ordered)) - 1)]


def _state(output):
    if not output.assessments:
        return "invalid_message" if output.result.status == "needs_input" else output.result.status
    return output.assessments[0].state


def evaluate_case(runner, case, fixture_version):
    profile = _profile(case)
    expected = case["expected"]
    trace_id = str(uuid.uuid4())
    output = runner.run(profile=profile, text=case.get("message"),
                        conversation_facts=case.get("conversation_facts"),
                        conversation_topic=case.get("conversation_topic"),
                        asked_fields=tuple(case.get("asked_fields") or ()),
                        expected_profile_version=case.get("expected_profile_version"), trace_id=trace_id)
    result = output.result
    asked = [q.field for q in result.follow_up_questions]
    confirmation_rules = ("conflict.", "invalid.")
    plain_asks = [q.field for q in result.follow_up_questions if not q.rule.startswith(confirmation_rules)]
    known_profile = {code for code, f in profile.fields.items() if f.status == "confirmed" and f.value is not None}
    stated = {**(case.get("conversation_facts") or {})}
    for assessment in output.assessments[:1]:
        stated.update({k: f.value for k, f in assessment.facts.items() if f.source == "conversation"})

    errors = []
    if len(asked) > MAX_QUESTIONS_PER_ROUND:
        errors.append(("too_many_questions", f"{len(asked)} 个问题"))
    repeated = [f for f in plain_asks if f in known_profile or f in stated]
    if repeated:
        errors.append(("repeated_ask", ", ".join(repeated)))
    unknown_in_profile = {code for code, f in profile.fields.items() if f.status != "confirmed"}
    for assessment in output.assessments:
        guessed = [k for k, f in assessment.facts.items() if f.source == "profile" and k in unknown_in_profile]
        if guessed:
            errors.append(("guessed_unknown", ", ".join(guessed)))
    must = {"destination_country", "visa_category"} & set(expected["required_missing"])
    if must and (result.status == "answered" or any(m.applicability == "required" for m in result.answer.materials)):
        errors.append(("definitive_list_without_must_have", ", ".join(sorted(must))))
    if expected.get("notice"):
        notice = next((n for n in result.notices if n.code == expected["notice"]), None)
        if notice is None or not notice.message.strip():
            errors.append(("unexplained_state", expected["notice"]))
    actual_state = _state(output)
    if actual_state != expected["state"]:
        errors.append(("state_mismatch", f"{actual_state} != {expected['state']}"))
    if result.status != expected["status"]:
        errors.append(("status_mismatch", f"{result.status} != {expected['status']}"))

    labelled = set(expected["required_missing"])
    identified = set(result.missing_fields)
    relevant = set(expected["relevant_fields"])
    return {
        "case_id": case["case_id"], "set": case["set"], "fixture_version": fixture_version,
        "trace_id": trace_id,
        "input": {k: case.get(k) for k in ("message", "profile", "conversation_facts", "conversation_topic",
                                           "asked_fields", "expected_profile_version") if case.get(k) is not None},
        "expected": expected,
        "actual": {"status": result.status, "state": actual_state, "missing_fields": result.missing_fields,
                   "questions": [{"field": q.field, "question_code": q.question_code, "rule": q.rule,
                                  "profile_version": q.profile_version} for q in result.follow_up_questions],
                   "notices": [n.model_dump() for n in result.notices]},
        "stage_ms": output.stage_ms,
        "counts": {"labelled_missing": len(labelled), "identified_missing": len(labelled & identified),
                   "spurious_missing": len(identified - labelled), "asked": len(asked),
                   "extra_asked": len([f for f in asked if f not in relevant])},
        "errors": [{"kind": k, "detail": d} for k, d in errors],
    }


def run(cases_path=DEFAULT_CASES, repeat=20):
    spec = json.loads(Path(cases_path).read_text(encoding="utf-8"))
    knowledge = FixtureKnowledgeRead.from_file(Path(cases_path).with_name(spec["knowledge_fixture"]))
    clock_value = datetime.fromisoformat(spec["clock"])
    runner = Agent2Runner(knowledge, lambda: clock_value)
    records = [evaluate_case(runner, case, spec["fixture_version"]) for case in spec["cases"]]

    rule_ms, model_ms = [], []
    started = time.perf_counter()
    for _ in range(repeat):
        for case in spec["cases"]:
            output = runner.run(profile=_profile(case), text=case.get("message"),
                                conversation_facts=case.get("conversation_facts"),
                                conversation_topic=case.get("conversation_topic"),
                                asked_fields=tuple(case.get("asked_fields") or ()),
                                expected_profile_version=case.get("expected_profile_version"))
            if output.assessments:
                rule_ms.append(output.stage_ms.get("m06_rules", 0.0))
                if "m06_model" in output.stage_ms:
                    model_ms.append(output.stage_ms["m06_model"])
    timing_wall_ms = (time.perf_counter() - started) * 1000

    def subset(name=None):
        return [r for r in records if name is None or r["set"] == name]

    def recall(rows):
        labelled = sum(r["counts"]["labelled_missing"] for r in rows)
        return 1.0 if labelled == 0 else sum(r["counts"]["identified_missing"] for r in rows) / labelled

    asked = sum(r["counts"]["asked"] for r in records)
    errors = {kind: sum(1 for r in records for e in r["errors"] if e["kind"] == kind) for kind in ERROR_KINDS}
    metrics = {
        "s1_recall": recall(subset("s1_uk_student_visa")),
        "baseline_recall": recall(subset("baseline_other_topics")),
        "overall_recall": recall(subset()),
        "extra_question_rate": 0.0 if asked == 0 else sum(r["counts"]["extra_asked"] for r in records) / asked,
        "questions_asked": asked,
        "spurious_missing": sum(r["counts"]["spurious_missing"] for r in records),
        "rule_p95_ms": round(_p95(rule_ms), 3),
        "rule_samples": len(rule_ms),
        "model_p95_ms": round(_p95(model_ms), 3) if model_ms else None,
        "errors": errors,
    }
    passed = {
        "s1_recall": metrics["s1_recall"] >= TARGETS["s1_recall"],
        "extra_question_rate": metrics["extra_question_rate"] <= TARGETS["extra_question_rate"],
        "rule_p95_ms": metrics["rule_p95_ms"] <= TARGETS["rule_p95_ms"],
        "zero_errors": not any(errors.values()),
    }
    return {"fixture_version": spec["fixture_version"], "knowledge_fixture": knowledge.fixture_version,
            "clock": spec["clock"], "repeat": repeat, "timing_wall_ms": round(timing_wall_ms, 1),
            "targets": TARGETS, "metrics": metrics, "passed": passed, "ok": all(passed.values()),
            "records": records}
