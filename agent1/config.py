"""本地部署配置：路径、环境变量、分类默认值（设计文档 2.5 / PRD 7.4）。"""
import json
import os
from pathlib import Path

from .environment import paths_for

ROOT = Path(__file__).resolve().parent.parent
SOURCES_PATH = ROOT / "sources.json"
LEGACY_MIXED_DB = ROOT / "data" / "agent1.sqlite3"


def configure_environment(environment=None):
    global PATHS, ENVIRONMENT, DATA, DB_PATH, LEGACY_DB_PATH
    global SNAPSHOT_DIR, GATE_DIR, PROBLEMS_PATH
    PATHS = paths_for(ROOT, environment or os.getenv("AGENT1_ENV", "development"))
    ENVIRONMENT = PATHS.environment
    DATA, DB_PATH = PATHS.data, PATHS.database
    LEGACY_DB_PATH = PATHS.legacy_database
    # Existing pipeline remains confined to a separate legacy namespace.
    SNAPSHOT_DIR, GATE_DIR = PATHS.legacy_snapshots, PATHS.legacy_gate
    PROBLEMS_PATH = GATE_DIR / "problems.json"
    return PATHS


configure_environment()

# Schema 与提示词版本：随处理逻辑升级而递增，用于受控重处理（设计 2.5）
SCHEMA_VERSION = "1.1.0"
PROMPT_VERSIONS = {"visa_policy": "visa-policies-8", "generic_facts": "generic-facts-1"}

# 各资料类别的默认检查周期与新鲜度 SLA（PRD 7.4：活动/费率/外汇 24h，签证/开户 7 天，学校费用入学季 7 天）
CATEGORY_DEFAULTS = {
    "visa": {"check_interval_minutes": 1440, "freshness_sla_hours": 168},
    "remittance": {"check_interval_minutes": 1440, "freshness_sla_hours": 24},
    "account": {"check_interval_minutes": 1440, "freshness_sla_hours": 168},
    "card": {"check_interval_minutes": 720, "freshness_sla_hours": 24},
    "school": {"check_interval_minutes": 1440, "freshness_sla_hours": 168},
    "fx": {"check_interval_minutes": 720, "freshness_sla_hours": 24},
}

LEASE_MINUTES = 20            # 运行租约时长，超过视为孤立运行可接管
RETRY_BASE_MINUTES = 5        # 失败重试退避基数（设计 A1.5：有限重试）
RETRY_MAX_MINUTES = 60
PROBLEMS_MAX_ENTRIES = 1000   # problems.json 保留上限


def ensure_dirs():
    require_legacy_environment()
    PATHS.validate()
    for path in (DATA, SNAPSHOT_DIR, GATE_DIR):
        path.mkdir(parents=True, exist_ok=True)


def require_legacy_environment():
    if ENVIRONMENT == "production":
        raise RuntimeError(
            "正式环境尚未接入新采集/发布服务。当前仅支持 db init / db status；"
            "旧管道只能在 development 或 test 环境使用。"
        )


def api_key():
    token = os.getenv("AGENT1_LLM_API_KEY")
    if token:
        return token
    try:
        from local_secrets import AGENT1_LLM_API_KEY
        return AGENT1_LLM_API_KEY
    except ImportError:
        return None


def _int_env(name, default):
    try:
        return int(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


def _float_env(name, default):
    try:
        return float(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


def _extra_body(model):
    """模型级额外参数。推理模型（deepseek 系）必须关闭思考模式：
    2026-10-12 实测 deepseek-v4.1-flash 默认思考时 token 全耗在 reasoning_content、
    finish_reason=length、正文为空；关闭后 5.2s 返回完整 JSON（快于 qwen3.8-flash 40 倍）。
    AGENT1_LLM_EXTRA_BODY 可传 JSON 覆盖，如 '{"enable_thinking": true}'。"""
    raw = os.getenv("AGENT1_LLM_EXTRA_BODY")
    body = json.loads(raw) if raw else {}
    if model and model.startswith("deepseek") and "enable_thinking" not in body:
        body["enable_thinking"] = False
    return body or None


def llm_settings():
    """LLM 接入参数。超时/重试默认值来自 2026-10-12 的实测：MaaS 工作区真实抽取
    首 token 延迟随输入增长（qwen3.8-flash 3000 字符约 185s），旧 60s 超时必失败。
    推荐模型 deepseek-v4.1-flash（关思考，实测 5-25s）。截取默认 2500 字符：
    GOV.UK 资金页全部关键事实位于前 2500 字节内，更短的截取可降低首 token 延迟。"""
    provider = os.getenv("AGENT1_LLM_PROVIDER")
    model = (provider or "").removeprefix("openai/") or None
    return {
        "provider": provider,
        "model": model,
        "base_url": os.getenv("AGENT1_LLM_BASE_URL"),
        "api_key": api_key(),
        "timeout": _float_env("AGENT1_LLM_TIMEOUT", 300),
        "max_retries": _int_env("AGENT1_LLM_MAX_RETRIES", 2),
        "excerpt_chars": _int_env("AGENT1_LLM_EXCERPT_CHARS", 2500),
        "extra_body": _extra_body(model),
    }
