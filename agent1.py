#!/usr/bin/env python
"""Agent1 CLI 入口。

完整实现位于 agent1/ 包（本地部署版）：
  来源登记 -> Crawl4AI 采集 -> LLM 按 Schema 抽取 -> 质量门控 -> 版本与受控发布。
新正式结构通过 db init 显式初始化；兼容管道仅限 development/test 环境。
历史混合数据保留在 data/agent1.sqlite3，本入口不会连接它。
"""
import sys

from agent1.cli import main

if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    raise SystemExit(main())
