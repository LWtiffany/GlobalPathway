"""Environment-driven LLM composition root."""

import os
import json
from dataclasses import dataclass

from .answer_writer import LlmAnswerWriter
from .contracts import AnswerWriterPort, UserUnderstandingPort
from .provider import OpenAICompatibleChatModel
from .understanding import LlmUserUnderstanding


@dataclass(frozen=True)
class LlmComponents:
    understanding: UserUnderstandingPort | None = None
    writer: AnswerWriterPort | None = None


def build_llm_components(env=None) -> LlmComponents:
    use_local_secrets = env is None
    env = os.environ if env is None else env
    setting = env.get("AGENT2_LLM_ENABLED", "auto").strip().lower()
    api_key = (env.get("AGENT2_LLM_API_KEY") or env.get("DASHSCOPE_API_KEY") or "").strip()
    base_url = (env.get("AGENT2_LLM_BASE_URL") or "").strip()
    if not api_key and use_local_secrets:
        try:
            from local_secrets import AGENT2_LLM_API_KEY
            api_key = AGENT2_LLM_API_KEY.strip()
        except (ImportError, AttributeError):
            pass
    if not base_url and use_local_secrets:
        try:
            from local_secrets import AGENT2_LLM_BASE_URL
            base_url = AGENT2_LLM_BASE_URL.strip()
        except (ImportError, AttributeError):
            pass
    local_model = None
    if use_local_secrets:
        try:
            from local_secrets import AGENT2_LLM_MODEL
            local_model = AGENT2_LLM_MODEL.strip()
        except (ImportError, AttributeError):
            pass
    enabled = bool(api_key) if setting == "auto" else setting in {"1", "true", "yes", "on"}
    if not enabled:
        return LlmComponents()
    if not api_key:
        raise RuntimeError("AGENT2_LLM_ENABLED is on but AGENT2_LLM_API_KEY is missing")
    if not base_url.startswith("https://"):
        raise RuntimeError("AGENT2_LLM_BASE_URL must be an HTTPS URL")
    model_name = env.get("AGENT2_LLM_MODEL") or local_model or "qwen3.8-flash"
    structured_mode = env.get("AGENT2_LLM_STRUCTURED_MODE") or \
        ("json_object" if model_name.startswith("deepseek") else "json_schema")
    raw_extra = env.get("AGENT2_LLM_EXTRA_BODY", "")
    extra_body = json.loads(raw_extra) if raw_extra else {}
    if model_name.startswith(("deepseek", "qwen")) and "enable_thinking" not in extra_body:
        extra_body["enable_thinking"] = False
    model = OpenAICompatibleChatModel(
        model_name=model_name,
        api_key=api_key,
        base_url=base_url,
        timeout_seconds=float(env.get("AGENT2_LLM_TIMEOUT_SECONDS", "60")),
        max_retries=int(env.get("AGENT2_LLM_MAX_RETRIES", "1")),
        structured_mode=structured_mode,
        extra_body=extra_body,
    )
    return LlmComponents(understanding=LlmUserUnderstanding(model), writer=LlmAnswerWriter(model))
