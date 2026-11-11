"""Environment-driven LLM composition root."""

import os
import json
from dataclasses import dataclass

from .answer_writer import LlmAnswerWriter
from .conversation_reasoner import LlmConversationReasoner
from .contracts import AnswerWriterPort, ConversationReasonerPort, UserUnderstandingPort
from .provider import OpenAICompatibleChatModel
from .understanding import LlmUserUnderstanding

# FX generic-answer rewrite: GLM-5.3 thinking mode (Aliyun Model Studio).
# Thinking cannot be disabled on this model; keep enable_thinking=true explicitly.
_DEFAULT_FX_ANSWER_MODEL = "glm-5.3"
_DEFAULT_FX_EXTRA_BODY: dict = {
    "enable_thinking": True,
    "reasoning_effort": "high",
}


@dataclass(frozen=True)
class LlmComponents:
    understanding: UserUnderstandingPort | None = None
    writer: AnswerWriterPort | None = None
    reasoner: ConversationReasonerPort | None = None
    # Dedicated structured model for FxWorkflow generic rewrite.
    fx_answer_model: OpenAICompatibleChatModel | None = None


def _secret(name: str, *, use_local: bool) -> str | None:
    if not use_local:
        return None
    try:
        import local_secrets
    except ImportError:
        return None
    value = getattr(local_secrets, name, None)
    text = str(value).strip() if value else ""
    return text or None


def _parse_extra_body(raw: str | None) -> dict:
    text = str(raw or "").strip()
    if not text:
        return {}
    try:
        payload = json.loads(text)
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _build_chat_model(
    *,
    model_name: str,
    api_key: str,
    base_url: str,
    structured_mode: str,
    extra_body: dict,
    timeout_seconds: float,
    max_retries: int,
) -> OpenAICompatibleChatModel:
    return OpenAICompatibleChatModel(
        model_name=model_name,
        api_key=api_key,
        base_url=base_url,
        timeout_seconds=timeout_seconds,
        max_retries=max_retries,
        structured_mode=structured_mode,
        extra_body=extra_body,
    )


def build_llm_components(env=None) -> LlmComponents:
    use_local_secrets = env is None
    env = os.environ if env is None else env
    setting = env.get("AGENT2_LLM_ENABLED", "auto").strip().lower()
    api_key = (env.get("AGENT2_LLM_API_KEY") or env.get("DASHSCOPE_API_KEY") or "").strip()
    base_url = (env.get("AGENT2_LLM_BASE_URL") or "").strip()
    if not api_key:
        api_key = (
            _secret("AGENT2_LLM_API_KEY", use_local=use_local_secrets)
            or _secret("AGENT1_LLM_API_KEY", use_local=use_local_secrets)
            or ""
        )
    if not base_url:
        base_url = (
            _secret("AGENT2_LLM_BASE_URL", use_local=use_local_secrets)
            or _secret("AGENT1_LLM_BASE_URL", use_local=use_local_secrets)
            or ""
        )
    local_model = _secret("AGENT2_LLM_MODEL", use_local=use_local_secrets)
    enabled = bool(api_key) if setting == "auto" else setting in {"1", "true", "yes", "on"}
    if not enabled:
        return LlmComponents()
    if not api_key:
        raise RuntimeError("AGENT2_LLM_ENABLED is on but AGENT2_LLM_API_KEY is missing")
    if not base_url.startswith("https://"):
        raise RuntimeError("AGENT2_LLM_BASE_URL must be an HTTPS URL")

    model_name = env.get("AGENT2_LLM_MODEL") or local_model or "deepseek-v4.1-flash"
    structured_mode = env.get("AGENT2_LLM_STRUCTURED_MODE") or (
        "json_object" if model_name.startswith("deepseek") else "json_schema"
    )
    extra_body = _parse_extra_body(env.get("AGENT2_LLM_EXTRA_BODY", ""))
    if model_name.startswith(("deepseek", "qwen")) and "enable_thinking" not in extra_body:
        extra_body["enable_thinking"] = False
    timeout_seconds = float(env.get("AGENT2_LLM_TIMEOUT_SECONDS", "30"))
    max_retries = int(env.get("AGENT2_LLM_MAX_RETRIES", "0"))

    model = _build_chat_model(
        model_name=model_name,
        api_key=api_key,
        base_url=base_url,
        structured_mode=structured_mode,
        extra_body=extra_body,
        timeout_seconds=timeout_seconds,
        max_retries=max_retries,
    )

    # FX generic answers stay on GLM-5.3 thinking, independent of understanding/writer.
    fx_model_name = (
        (env.get("AGENT2_FX_LLM_MODEL") or "").strip()
        or _secret("AGENT2_FX_LLM_MODEL", use_local=use_local_secrets)
        or _DEFAULT_FX_ANSWER_MODEL
    )
    fx_extra = _parse_extra_body(env.get("AGENT2_FX_LLM_EXTRA_BODY"))
    if not fx_extra:
        fx_extra = dict(_DEFAULT_FX_EXTRA_BODY)
    else:
        fx_extra = {**fx_extra, "enable_thinking": True}
        fx_extra.setdefault("reasoning_effort", "high")
    fx_timeout = float(env.get("AGENT2_FX_LLM_TIMEOUT_SECONDS", "90") or "90")
    fx_structured = (env.get("AGENT2_FX_LLM_STRUCTURED_MODE") or "json_object").strip().lower()
    if fx_structured not in {"json_object", "json_schema"}:
        fx_structured = "json_object"
    fx_answer_model = _build_chat_model(
        model_name=fx_model_name,
        api_key=api_key,
        base_url=base_url,
        structured_mode=fx_structured,
        extra_body=fx_extra,
        timeout_seconds=fx_timeout,
        max_retries=max_retries,
    )

    return LlmComponents(
        understanding=LlmUserUnderstanding(model),
        writer=LlmAnswerWriter(model),
        reasoner=LlmConversationReasoner(model),
        fx_answer_model=fx_answer_model,
    )
