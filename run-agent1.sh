#!/usr/bin/env bash
# Agent1 本地运行入口（macOS / Linux，等价于 Windows 的「触发测试.bat」）
#
# 用法：
#   ./run-agent1.sh                      # 抓取默认来源 uk_student_visa_money
#   ./run-agent1.sh uk_student_visa      # 指定来源
#   ./run-agent1.sh uk_student_visa_money --force
#   ./run-agent1.sh sources list         # 任意 agent1 子命令都可用
#
# 需要先在 local_secrets.py 中填入 AGENT1_LLM_API_KEY。
set -euo pipefail

cd "$(dirname "$0")"

PY=".venv/bin/python"
if [ ! -x "$PY" ]; then
  echo "[错误] 找不到虚拟环境：$PY"
  echo "请先执行：python3 -m venv .venv && .venv/bin/python -m pip install -r requirements.txt"
  exit 1
fi

# 始终使用 development 环境（与 触发测试.bat 一致，不会误用 production）
export AGENT1_ENV="${AGENT1_ENV:-development}"

# LLM 接入（阿里云 MaaS 工作区）。可被外部环境变量覆盖。
export AGENT1_LLM_PROVIDER="${AGENT1_LLM_PROVIDER:-openai/deepseek-v4.1-flash}"
export AGENT1_LLM_BASE_URL="${AGENT1_LLM_BASE_URL:-https://ws-6noevvymaytxig7l.cn-beijing.maas.aliyuncs.com/compatible-mode/v1}"

# Crawl4AI 的缓存/robots 目录必须落在项目内（沙箱下用户主目录无法使用 SQLite WAL）
export CRAWL4_AI_BASE_DIRECTORY="${CRAWL4_AI_BASE_DIRECTORY:-$PWD/data/_crawl4ai}"

# 第一个参数若是已知子命令则原样转发；否则视为「来源 ID」，自动补 `run`
# （与「触发测试.bat」一致：触发测试.bat uk_student_visa 等价于 agent1.py run uk_student_visa）
KNOWN_COMMANDS="db scan sources run schedule runs gate review withdraw rollback knowledge status visa show"

if [ "$#" -eq 0 ]; then
  set -- run uk_student_visa_money
elif ! printf '%s\n' $KNOWN_COMMANDS | grep -qx "$1"; then
  set -- run "$@"
fi

exec "$PY" agent1.py "$@"
