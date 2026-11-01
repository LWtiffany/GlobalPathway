#!/usr/bin/env python
"""信用卡「追问 → 给建议」本地演示脚本（连真实 RDS，只读）。

运行方式（在项目根目录）:
    .venv/Scripts/python.exe scripts/demo_credit_card.py

数据安全:
    - 产品数据来自 RDS 的 CREDIT_CARD 系列表，只读，零写入。
    - 会话/档案使用远程 PostgreSQL；演示数据与正式数据使用相同存储协议。
    - 依赖 SSH 隧道: 先跑 scripts/start_team03_pg_tunnel.ps1 打通 127.0.0.1:15432。

设计说明:
    - 追问流程由 RDS 产品规则驱动（如新增居民资格规则会多问一步），
      脚本按「当前问题实际问了什么」自动作答，不硬编码对话步骤。
    - 默认人设: 香港 / 18+ / 最高收入档 / 香港居民 / 全额还款 / 指定消费场景 /
      月消费 5k-10k / 无奖励偏好。

可选参数:
    --scenarios online,travel   消费场景（逗号分隔，默认 online,travel）
    --text "..."                场景 B 的自由文本（覆盖默认输入）
"""
import argparse
import asyncio
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent2.knowledge import FixtureKnowledgeRead
from agent2.llm.factory import LlmComponents
from agent2.orchestration.domains.credit_card_engine import recommend
from agent2.orchestration.domains.credit_card_repository import RdsCreditCardRepository
from agent2.service import Agent2Service
from agent2.store import Store

NOW = datetime.now(timezone.utc)
BAR = "=" * 72
MAX_ROUNDS = 10

# 每个追问字段的自动作答策略: 返回选项列表中应选的 option dict
def _pick(options, value, fallback_index=None):
    for opt in options:
        if opt["value"] == value:
            return opt
    return options[fallback_index if fallback_index is not None else 0]

ANSWER_POLICY = {
    "application_region": lambda opts: _pick(opts, "CN-HK"),
    "applicant_age_band": lambda opts: _pick(opts, "18_or_over"),
    "annual_income_band_hkd": lambda opts: opts[-1],           # 最高收入档
    "hk_resident": lambda opts: _pick(opts, True),
    "hsbc_premier_path": lambda opts: _pick(opts, "not_applicable"),
    "hsbc_relationship_balance_band": lambda opts: opts[len(opts) // 2],
    "asset_band": lambda opts: _pick(opts, "500k_plus"),
    "repayment_mode": lambda opts: _pick(opts, "full"),
    "mainland_payment_method": lambda opts: _pick(opts, "mobile_or_qr"),
    "monthly_card_spend_band_hkd": lambda opts: _pick(opts, "5k_10k", len(opts) // 2),
    "reward_preference": lambda opts: _pick(opts, "no_preference", len(opts) - 1),
}


def render_reply(r):
    print(f"  [状态] {r.status}    (档案版本 v{r.profile_version})")
    print(f"  [摘要] {r.answer.summary}")
    for step in r.answer.steps:
        print(f"         - {step}")
    for lim in r.answer.limitations:
        print(f"         （说明）{lim}")
    for q in r.follow_up_questions:
        kind = "多选" if q.input_type == "multi_select" else "单选"
        labels = " / ".join(opt["label"] for opt in q.options)
        print(f"    追问 · {q.text}  [{kind}]")
        print(f"           →  {labels}")
    if r.next_actions:
        print(f"  [建议] {'; '.join(r.next_actions)}")
    if r.notices:
        print(f"  [提示] {'; '.join(f'{n.code}: {n.message}' for n in r.notices)}")


def auto_answer(question, scenarios):
    """按策略回答一个追问问题，返回 (字段, 选中值, 中文标签)。"""
    if question.field == "spending_scenarios":
        wanted = [s for s in scenarios
                  if any(opt["value"] == s for opt in question.options)]
        chosen = wanted or [question.options[0]["value"]]
        label = "、".join(opt["label"] for opt in question.options
                          if opt["value"] in chosen)
        return question.field, chosen, label
    policy = ANSWER_POLICY.get(question.field)
    if policy is None:  # 未知新字段: 单选取首项，多选取首两项
        if question.input_type == "multi_select":
            chosen = [opt["value"] for opt in question.options[:2]]
            label = "、".join(opt["label"] for opt in question.options[:2])
        else:
            chosen = question.options[0]["value"]
            label = question.options[0]["label"]
        print(f"    （未知字段 {question.field}，按默认策略作答）")
        return question.field, chosen, label
    opt = policy(question.options)
    return question.field, opt["value"], opt["label"]


def run_conversation(service, user, conv, opening_text, scenarios, prefix):
    """发起对话并自动回答系统实际追问的每一轮问题，直到给出结论。"""
    reply = service.post_message(user, conv, opening_text, f"{prefix}0")
    render_reply(reply)
    for round_no in range(1, MAX_ROUNDS + 1):
        if reply.status != "needs_input" or not reply.follow_up_questions:
            return reply
        confirmed, labels = {}, []
        for q in reply.follow_up_questions:
            field, value, label = auto_answer(q, scenarios)
            confirmed[field] = value
            labels.append(label)
        print(f"\n[用户] 选择：{'；'.join(labels)}\n")
        reply = service.post_message(
            user, conv, f"（第 {round_no} 轮作答）", f"{prefix}{round_no}",
            confirmed_fields=confirmed,
            expected_profile_version=reply.profile_version)
        render_reply(reply)
    return reply


def show_structured(scenarios):
    """底层评分引擎的透明产出（只读 RDS，无副作用）。"""

    async def _run():
        cat = await RdsCreditCardRepository().active_products("CN-HK", NOW)
        result = recommend(
            cat.products,
            {"applicant_age": 25, "annual_income_hkd": 300000,
             "repayment_mode": "full", "hk_resident": True},
            scenarios, "no_preference")
        print(f"  market: {cat.market}   产品总数: {len(cat.products)}")
        p = result.primary
        if p:
            print(f"  [主推荐] {p.product_name} ({p.card_id})  年度净值={p.score} HKD  "
                  f"资格={p.eligibility.status}  覆盖={p.matched_scenarios}")
            print(f"           分数明细: {p.score_breakdown}")
        for s in result.supplements:
            print(f"  [补充卡] {s.product_name} ({s.card_id})  年度净值={s.score} HKD  "
                  f"覆盖={s.matched_scenarios}")
        for a in result.alternatives:
            print(f"  [免年费备选] {a.product_name} ({a.card_id})  年度净值={a.score} HKD")
        if result.excluded:
            print(f"  [排除(不达标)] {list(result.excluded)}")

    asyncio.run(_run())


def main():
    parser = argparse.ArgumentParser(description="信用卡追问/推荐演示")
    parser.add_argument("--scenarios", default="online,travel",
                        help="消费场景码，逗号分隔，可选: online/dining/travel/"
                             "overseas/mainland_china/tuition/retail（默认 online,travel）")
    parser.add_argument("--text", default=None,
                        help="场景 B 的自由文本（默认给足信息的句子）")
    args = parser.parse_args()

    scenarios = [s.strip() for s in args.scenarios.split(",") if s.strip()]
    scenario_text = "、".join(scenarios)
    text_b = args.text or "我在香港想办信用卡，年薪30万，每月全额还款，主要网购和旅行"

    store = Store()  # 远程 PostgreSQL（不再回退到 SQLite）
    try:
        service = Agent2Service(
            store, FixtureKnowledgeRead.from_file(), clock=lambda: NOW,
            credit_card_repository=RdsCreditCardRepository(),
            llm_components=LlmComponents(),
        )
        user = service.demo_login("demo-rds")["user_id"]
        profile = service.create_profile(user, synthetic=True)
        conv = service.create_conversation(user, profile.profile_id)["conversation_id"]

        print(BAR)
        print("场景 A：用户零信息起步，系统按真实 RDS 门槛逐步追问（自动作答）")
        print(BAR)
        print("\n[用户] 我想申请一张信用卡\n")
        run_conversation(service, user, conv, "我想申请一张信用卡",
                         scenarios, "a")

        print("\n" + BAR)
        print(f"场景 A 底层评分引擎产出（场景: {scenario_text}，结构化，只读 RDS）")
        print(BAR)
        show_structured(scenarios)

        # 场景 B：一次性给足信息，剩余字段由系统按 RDS 规则动态追问
        print("\n" + BAR)
        print("场景 B：用户直接给足信息，系统自动抽取、动态补问剩余字段")
        print(BAR)
        profile2 = service.create_profile(user, synthetic=True)
        conv2 = service.create_conversation(user, profile2.profile_id)["conversation_id"]
        print(f"\n[用户] {text_b}\n")
        run_conversation(service, user, conv2, text_b, scenarios, "b")

        print("\n" + BAR)
        print("演示结束（会话数据在内存中，进程结束即销毁；RDS 全程只读零写入）")
        print(BAR)
    finally:
        store.close()


if __name__ == "__main__":
    main()
