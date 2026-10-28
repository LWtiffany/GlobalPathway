#!/usr/bin/env python
"""agent2 命令行聊天（交互式试用整个 agent）。

随便聊任何主题，planner 自动路由: 信用卡推荐、签证材料、跨境汇款、香港开户等。

运行方式（项目根目录）:
    .venv/Scripts/python.exe scripts/chat.py            # 纯规则（确定性）
    .venv/Scripts/python.exe scripts/chat.py --llm      # LLM 增强（理解+改写）

前提:
    先开 SSH 隧道（信用卡/账户产品数据走真实 RDS，只读）:
    PowerShell:  scripts/start_team03_pg_tunnel.ps1

数据安全:
    - 产品数据来自 RDS，只读，零写入。
    - 会话/档案使用内存 SQLite，进程结束即销毁，不留任何本地文件。

交互:
    - 开场随便说，例如:
        「我想在香港办一张信用卡，年薪30万，主要网购和旅行」
        「我准备去英国读硕士，签证需要准备什么材料？」
        「我要给学校交学费，怎么从内地汇款到英国？」
    - 追问有两种: 编号选项（输入编号，多选用空格/逗号分隔如 1 3）或自由文本（直接输入）。
    - 命令: /quit 退出 · /restart 重开会话（档案清零）。
"""
import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent2.knowledge import FixtureKnowledgeRead
from agent2.llm.factory import LlmComponents, build_llm_components
from agent2.orchestration.domains.credit_card_repository import RdsCreditCardRepository
from agent2.service import Agent2Service
from agent2.store import Store

NOW = datetime.now(timezone.utc)
BAR = "=" * 64
QUIT = {"/quit", "/q", "exit", "退出"}


def parse_ints(raw: str) -> list[int] | None:
    """把 '1' / '1 3' / '1，3' 解析成编号列表；非法输入返回 None。"""
    tokens = raw.replace(",", " ").replace("，", " ").replace("、", " ").split()
    try:
        return [int(token) for token in tokens]
    except ValueError:
        return None


def ask_question(question, idx: int, total: int):
    """渲染一个追问并等待作答，返回 (字段值, 展示文本)；/restart 返回 None。

    选项题输编号；文本题直接输入内容。
    """
    multi = question.input_type == "multi_select"
    while True:
        if question.options:
            kind = "多选" if multi else "单选"
            hint = "（可输入多项，空格或逗号分隔）" if multi else ""
            print(f"\n问题 {idx}/{total}：{question.text}  [{kind}]{hint}")
            for number, option in enumerate(question.options, 1):
                print(f"  {number}) {option['label']}")
            try:
                raw = input("你的选择: ").strip()
            except (EOFError, KeyboardInterrupt):
                raise SystemExit(0)
        else:
            print(f"\n问题 {idx}/{total}：{question.text}  [输入文本]")
            try:
                raw = input("你的回答: ").strip()
            except (EOFError, KeyboardInterrupt):
                raise SystemExit(0)
            if raw.lower() in QUIT:
                raise SystemExit(0)
            if raw == "/restart":
                return None
            if not raw:
                print("  （请输入内容）")
                continue
            return raw, raw
        if raw.lower() in QUIT:
            raise SystemExit(0)
        if raw == "/restart":
            return None
        if not raw:
            continue
        picks = parse_ints(raw)
        if picks is None:
            print("  （请输入选项编号，例如 1，多选用 1 3）")
            continue
        if any(pick < 1 or pick > len(question.options) for pick in picks):
            print(f"  （编号需在 1–{len(question.options)} 之间）")
            continue
        if not multi and len(picks) > 1:
            picks = picks[:1]
            print("  （单选只取第一项）")
        options = [question.options[pick - 1] for pick in dict.fromkeys(picks)]
        if multi:
            return [option["value"] for option in options], \
                   "、".join(option["label"] for option in options)
        return options[0]["value"], options[0]["label"]


def render_reply(reply):
    print(f"\n[助手] ({reply.status})  {reply.answer.summary}")
    for step in reply.answer.steps:
        print(f"  - {step}")
    for limitation in reply.answer.limitations:
        print(f"  （说明）{limitation}")
    for notice in reply.notices:
        print(f"  [提示] {notice.code}: {notice.message}")
    if reply.next_actions:
        print(f"  [下一步] {'; '.join(reply.next_actions)}")


def main():
    parser = argparse.ArgumentParser(description="agent2 命令行聊天（试用整个 agent）")
    parser.add_argument("--llm", action="store_true",
                        help="启用 LLM 增强（自由文本理解 + 答案改写）")
    args = parser.parse_args()

    llm = (build_llm_components() if args.llm else LlmComponents())
    llm_note = ("LLM 增强已启用（qwen）" if llm.understanding is not None
                else "纯规则模式（确定性）")

    store = Store()  # 内存 SQLite，退出即销毁
    try:
        service = Agent2Service(
            store, FixtureKnowledgeRead.from_file(), clock=lambda: NOW,
            credit_card_repository=RdsCreditCardRepository(),
            llm_components=llm)
        user = service.demo_login("cli-user")["user_id"]
        profile = service.create_profile(user, synthetic=True)
        conversation = service.create_conversation(
            user, profile.profile_id)["conversation_id"]

        print(BAR)
        print("HSBC 留学准备助手 · agent2 命令行版")
        print(BAR)
        print(f"模式: {llm_note}")
        print("数据: 真实 RDS（只读）· 会话: 内存（退出即销毁）")
        print("可以聊: 信用卡推荐 / 签证材料 / 跨境汇款 / 香港开户 / 境外消费等")
        print("命令: /quit 退出 · /restart 重开会话")
        print(BAR)

        counter = 0
        while True:
            try:
                text = input("\n[你] ").strip()
            except (EOFError, KeyboardInterrupt):
                break
            if not text:
                continue
            if text.lower() in QUIT:
                break
            if text == "/restart":
                profile = service.create_profile(user, synthetic=True)
                conversation = service.create_conversation(
                    user, profile.profile_id)["conversation_id"]
                print("（已重开会话，档案清零）")
                continue

            counter += 1
            try:
                reply = service.post_message(user, conversation, text, f"cli{counter}")
            except Exception as error:  # RDS 未开隧道等：给出可操作的提示
                print(f"\n[系统] 调用失败: {error}")
                print("        若是数据库连接问题，请先开 SSH 隧道:")
                print("        scripts/start_team03_pg_tunnel.ps1")
                continue
            render_reply(reply)

            # 追问循环：渲染每个问题 → 一次性提交全部回答
            while reply.status == "needs_input" and reply.follow_up_questions:
                confirmed, labels = {}, []
                restarted = False
                for idx, question in enumerate(reply.follow_up_questions, 1):
                    answer = ask_question(question, idx,
                                          len(reply.follow_up_questions))
                    if answer is None:  # /restart
                        restarted = True
                        break
                    value, label = answer
                    confirmed[question.field] = value
                    labels.append(label)
                if restarted:
                    break
                counter += 1
                reply = service.post_message(
                    user, conversation, "；".join(labels), f"cli{counter}",
                    confirmed_fields=confirmed,
                    expected_profile_version=reply.profile_version)
                render_reply(reply)

            if reply.status == "cannot_answer":
                print("\n（当前条件无法给出结论；输入 /restart 重开档案重新作答，或 /quit 退出）")
            elif reply.status == "out_of_scope":
                print("\n（换个说法试试，或 /restart 重开；/quit 退出）")
            elif reply.status in ("answered", "needs_verification"):
                print("\n（可继续追问或换主题，例如「如果主要在大陆消费呢？」；/quit 退出）")
        print("\n再见（会话数据已随进程销毁，RDS 全程只读）。")
    finally:
        store.close()


if __name__ == "__main__":
    main()
