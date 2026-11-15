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


def _structured_mode(env, model_name: str) -> str:
    # Explicit global override wins; otherwise infer per model name.
    return env.get("AGENT2_LLM_STRUCTURED_MODE") or \
        ("json_object" if model_name.startswith("deepseek") else "json_schema")


def _extra_body(env, model_name: str, override_env_name: str) -> dict:
    # Per-port override wins; otherwise the global AGENT2_LLM_EXTRA_BODY applies.
    raw = env.get(override_env_name) or env.get("AGENT2_LLM_EXTRA_BODY") or ""
    extra_body = json.loads(raw) if raw else {}
    if model_name.startswith(("deepseek", "qwen", "kimi")) and "enable_thinking" not in extra_body:
        extra_body["enable_thinking"] = False
    return extra_body


def _parse_extra_body(raw: str | None) -> dict:
    text = str(raw or "").strip()
    if not text:
        return {}
    try:
        payload = json.loads(text)
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


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
    enabled = bool(api_key) if setting == "auto" else setting in {"1", "true", "yes", "on"}
    if not enabled:
        return LlmComponents()
    if not api_key:
        raise RuntimeError("AGENT2_LLM_ENABLED is on but AGENT2_LLM_API_KEY is missing")
    if not base_url.startswith("https://"):
        raise RuntimeError("AGENT2_LLM_BASE_URL must be an HTTPS URL")

    def env_or_local(name, default=None):
        # Local dev keeps overrides in local_secrets.py next to env vars; explicit
        # environment always wins, matching the key/base_url fallback behaviour.
        value = env.get(name)
        if not value and use_local_secrets:
            value = _secret(name, use_local=True)
        return value or default

    main_model = env_or_local("AGENT2_LLM_MODEL", "deepseek-v4.1-flash")
    timeout_seconds = float(env.get("AGENT2_LLM_TIMEOUT_SECONDS", "30"))
    max_retries = int(env.get("AGENT2_LLM_MAX_RETRIES", "0"))

    def build_model(model_name: str, extra_body_env_name: str,
                    *, structured_mode: str | None = None,
                    timeout: float | None = None,
                    extra_body: dict | None = None) -> OpenAICompatibleChatModel:
        return OpenAICompatibleChatModel(
            model_name=model_name,
            api_key=api_key,
            base_url=base_url,
            timeout_seconds=timeout if timeout is not None else timeout_seconds,
            max_retries=max_retries,
            structured_mode=structured_mode or _structured_mode(env, model_name),
            extra_body=extra_body if extra_body is not None
            else _extra_body(env, model_name, extra_body_env_name),
        )

    # Ports may run different tiers (2026-11 A/B): fast flash for understanding
    # and reasoning, kimi-k3 for answer writing. Unset per-port vars fall back
    # to AGENT2_LLM_MODEL.
    understanding_model = build_model(
        env_or_local("AGENT2_UNDERSTANDING_MODEL") or main_model,
        "AGENT2_UNDERSTANDING_EXTRA_BODY")
    writer_model = build_model(
        env_or_local("AGENT2_WRITER_MODEL") or main_model,
        "AGENT2_WRITER_EXTRA_BODY")
    reasoner_model = build_model(
        env_or_local("AGENT2_REASONER_MODEL") or main_model,
        "AGENT2_REASONER_EXTRA_BODY")

    # FX generic answers stay on GLM-5.3 thinking, independent of understanding/writer.
    fx_model_name = (
        env_or_local("AGENT2_FX_LLM_MODEL") or _DEFAULT_FX_ANSWER_MODEL
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
    fx_answer_model = build_model(
        fx_model_name,
        "AGENT2_FX_LLM_EXTRA_BODY",
        structured_mode=fx_structured,
        timeout=fx_timeout,
        extra_body=fx_extra,
    )

    return LlmComponents(
        understanding=LlmUserUnderstanding(understanding_model),
        writer=LlmAnswerWriter(writer_model),
        reasoner=LlmConversationReasoner(reasoner_model),
        fx_answer_model=fx_answer_model,
    )
