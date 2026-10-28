"""Benchmark Agent2 LLM paths against the configured Alibaba Model Studio workspace.

Run after changing the network/VPN state:
    python scripts/benchmark_agent2_llm.py

The script times every request, validates the structured result, prints a summary,
and writes a JSON report under data/agent2/eval/. It never prints the API key.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent2 import clarify  # noqa: E402
from agent2.llm.answer_writer import LlmAnswerWriter  # noqa: E402
from agent2.llm.provider import OpenAICompatibleChatModel  # noqa: E402
from agent2.llm.understanding import LlmUserUnderstanding  # noqa: E402
from agent2.llm.validation import validate_understanding, validate_written_answer  # noqa: E402
from agent2.models import AnswerBody  # noqa: E402
from agent2.orchestration.contracts import (DomainResult, OrchestrationInput,  # noqa: E402
                                            UnifiedAnswer)


@dataclass(frozen=True)
class Target:
    model: str
    structured_mode: str
    extra_body: dict


@dataclass
class Measurement:
    model: str
    structured_mode: str
    operation: str
    attempt: int
    ok: bool
    elapsed_seconds: float
    error_type: str | None = None
    error_message: str | None = None


TARGETS = (
    Target("deepseek-v4.1-flash", "json_object", {"enable_thinking": False}),
    Target("qwen3.8-flash", "json_schema", {"enable_thinking": False}),
)


def credentials():
    api_key = os.getenv("AGENT2_LLM_API_KEY") or os.getenv("DASHSCOPE_API_KEY")
    base_url = os.getenv("AGENT2_LLM_BASE_URL")
    if not api_key or not base_url:
        try:
            import local_secrets
        except ImportError as exc:
            raise RuntimeError("缺少 Agent2 LLM 凭据") from exc
        api_key = api_key or getattr(local_secrets, "AGENT2_LLM_API_KEY", None)
        base_url = base_url or getattr(local_secrets, "AGENT2_LLM_BASE_URL", None)
    if not api_key:
        raise RuntimeError("缺少 AGENT2_LLM_API_KEY")
    if not base_url or not base_url.startswith("https://"):
        raise RuntimeError("缺少有效的 AGENT2_LLM_BASE_URL")
    return api_key, base_url


def representative_request():
    return OrchestrationInput(
        text="我是在香港读书的全日制学生，年收入大约五万港币，平时主要网购和餐饮，每月全额还款，想申请一张信用卡。",
        profile_id="benchmark-profile",
        profile_version=1,
        profile_values={"application_region": "CN-HK"},
    )


def representative_answer():
    domain = DomainResult(
        task_id="task-1",
        domain="credit_card",
        status="answered",
        answer=AnswerBody(
            summary="主推荐：HSBC Red Credit Card",
            steps=[
                "主推荐：HSBC Red Credit Card；匹配场景：网购、餐饮；资格：可提交资产证明，需 HSBC 个案审批。",
                "备选：HSBC Student Visa Gold Card；符合公开学生申请路径，最终仍以银行审批为准。",
            ],
            limitations=["申请结果、信用额度和最终条款以 HSBC 审批及官网最新资料为准。"],
        ),
        evidence_refs=["https://www.hsbc.com.hk/credit-cards/compare/"],
        payload={
            "recommendations": {
                "primary": {"card_id": "red", "product_name": "HSBC Red Credit Card",
                            "eligibility": {"status": "needs_bank_assessment"}},
                "alternatives": [{"card_id": "student-gold",
                                  "product_name": "HSBC Student Visa Gold Card",
                                  "eligibility": {"status": "eligible"}}],
            }
        },
    )
    return UnifiedAnswer(status="answered", answer=domain.answer, domain_results=[domain])


def safe_error(exc: Exception):
    message = str(exc).replace("\n", " ")[:300]
    return type(exc).__name__, message


async def timed(operation, attempt, target, coroutine, timeout):
    started = time.perf_counter()
    try:
        await asyncio.wait_for(coroutine, timeout=timeout)
        return Measurement(target.model, target.structured_mode, operation, attempt, True,
                           round(time.perf_counter() - started, 3))
    except Exception as exc:
        error_type, error_message = safe_error(exc)
        return Measurement(target.model, target.structured_mode, operation, attempt, False,
                           round(time.perf_counter() - started, 3), error_type, error_message)


async def run_target(target, repeat, timeout, api_key, base_url):
    model = OpenAICompatibleChatModel(
        model_name=target.model,
        api_key=api_key,
        base_url=base_url,
        timeout_seconds=timeout,
        max_retries=0,
        structured_mode=target.structured_mode,
        extra_body=target.extra_body,
    )
    understanding = LlmUserUnderstanding(model)
    writer = LlmAnswerWriter(model)
    request = representative_request()
    verified = representative_answer()
    catalog = clarify.field_catalog()
    results = []
    for attempt in range(1, repeat + 1):
        async def understand_and_validate():
            raw = await understanding.understand(request, catalog)
            validate_understanding(raw, request, set(catalog), clarify.validate)

        results.append(await timed("understanding", attempt, target,
                                   understand_and_validate(), timeout))
        async def write_and_validate():
            written = await writer.write(request=request, verified_answer=verified)
            validate_written_answer(written, verified.answer)

        results.append(await timed("answer_writer", attempt, target,
                                   write_and_validate(), timeout))
    return results


def percentile(values, fraction):
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, round((len(ordered) - 1) * fraction)))
    return ordered[index]


def summarize(measurements):
    groups = {}
    for item in measurements:
        groups.setdefault((item.model, item.structured_mode, item.operation), []).append(item)
    summary = []
    for (model, mode, operation), items in groups.items():
        successful = [item.elapsed_seconds for item in items if item.ok]
        summary.append({
            "model": model,
            "structured_mode": mode,
            "operation": operation,
            "successes": len(successful),
            "failures": len(items) - len(successful),
            "mean_seconds": round(statistics.mean(successful), 3) if successful else None,
            "p50_seconds": percentile(successful, .50),
            "p95_seconds": percentile(successful, .95),
        })
    return summary


async def main_async(args):
    api_key, base_url = credentials()
    selected = [target for target in TARGETS if not args.model or target.model in args.model]
    measurements = []
    for target in selected:
        print(f"正在测试 {target.model} / {target.structured_mode}，每个模块 {args.repeat} 次……",
              flush=True)
        target_results = await run_target(target, args.repeat, args.timeout, api_key, base_url)
        measurements.extend(target_results)
        for item in target_results:
            state = "成功" if item.ok else f"失败（{item.error_type}）"
            print(f"  {item.operation} 第 {item.attempt} 次：{state}，{item.elapsed_seconds:.3f} 秒",
                  flush=True)

    created_at = datetime.now(timezone.utc)
    report = {
        "created_at": created_at.isoformat(),
        "network_note": args.network_note,
        "repeat": args.repeat,
        "timeout_seconds": args.timeout,
        "summary": summarize(measurements),
        "measurements": [asdict(item) for item in measurements],
    }
    output = Path(args.output) if args.output else \
        ROOT / "data" / "agent2" / "eval" / f"llm-benchmark-{created_at:%Y%m%dT%H%M%SZ}.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print("\n汇总：")
    for item in report["summary"]:
        print(f"  {item['model']} / {item['operation']}: "
              f"成功 {item['successes']}，失败 {item['failures']}，"
              f"平均 {item['mean_seconds']} 秒，P95 {item['p95_seconds']} 秒")
    print(f"\n报告已保存：{output}")
    return 0 if all(item.ok for item in measurements) else 1


def main(argv=None):
    parser = argparse.ArgumentParser(description="计时测试 Agent2 阿里云 LLM 的真实 Prompt")
    parser.add_argument("--repeat", type=int, default=1, help="每个模型、每个模块的次数（默认 1）")
    parser.add_argument("--timeout", type=float, default=300, help="单次请求超时秒数（默认 300）")
    parser.add_argument("--model", action="append", choices=[target.model for target in TARGETS],
                        help="只测试指定模型，可重复传入；默认全部")
    parser.add_argument("--network-note", default="VPN off", help="写入报告的网络状态说明")
    parser.add_argument("--output", help="报告输出路径")
    args = parser.parse_args(argv)
    if args.repeat < 1:
        parser.error("--repeat 必须大于等于 1")
    if args.timeout <= 0:
        parser.error("--timeout 必须大于 0")
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
