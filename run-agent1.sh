#!/usr/bin/env bash
# Agent1 本地运行入口（macOS / Linux，等价于 Windows 的「触发测试.bat」）
#
# 用法：
#   ./run-agent1.sh                      # 增量抓取 UK / US / CN-HK 全部签证来源（scan-region all）
#   ./run-agent1.sh UK                   # 只抓一个区域（UK / US / CN-HK）
#   ./run-agent1.sh uk_student_visa      # 指定来源（同区域其他页面沿用上次快照）
#   ./run-agent1.sh UK --force           # 强制全量重抽（--full）
#   ./run-agent1.sh review list           # 任意 agent1 子命令都可用
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
export AGENT1_LLM_BASE_URL="${AGENT1_LLM_BASE_URL:-https://llm-tlndh47wd9ustbss.cn-beijing.maas.aliyuncs.com/compatible-mode/v1}"

# Crawl4AI 的缓存/robots 目录必须落在项目内（沙箱下用户主目录无法使用 SQLite WAL）
export CRAWL4_AI_BASE_DIRECTORY="${CRAWL4_AI_BASE_DIRECTORY:-$PWD/data/_crawl4ai}"

# 第一个参数若是已知子命令则原样转发；否则视为「来源 ID」，自动补 `scan`
# （新 schema 管道：抽取结果直写 domain 表；变更/门控失败进 REVIEW_QUEUE 人工审核）
KNOWN_COMMANDS="db scan scan-region review publish publication sources run schedule runs gate knowledge status visa show"
REGIONS="all UK US CN-HK"

if [ "$#" -eq 0 ]; then
  set -- scan-region all
elif printf '%s\n' $REGIONS | grep -qx "$1"; then
  set -- scan-region "$@"
elif ! printf '%s\n' $KNOWN_COMMANDS | grep -qx "$1"; then
  set -- scan "$@"
fi

# --force 是 --full 的别名
args=()
for arg in "$@"; do
  if [ "$arg" = "--force" ] && { [ "$1" = "scan" ] || [ "$1" = "scan-region" ]; }; then
    args+=("--full")
  else
    args+=("$arg")
  fi
done
set -- "${args[@]}"

exec "$PY" agent1.py "$@"
