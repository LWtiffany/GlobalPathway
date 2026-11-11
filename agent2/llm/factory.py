"""Environment-driven LLM composition root."""

import os
import json
from dataclasses import dataclass

from .answer_writer import LlmAnswerWriter
from .conversation_reasoner import LlmConversationReasoner
from .contracts import AnswerWriterPort, ConversationReasonerPort, UserUnderstandingPort
from .provider import OpenAICompatibleChatModel
from .understanding import LlmUserUnderstanding


@dataclass(frozen=True)
class LlmComponents:
    understanding: UserUnderstandingPort | None = None
    writer: AnswerWriterPort | None = None
    reasoner: ConversationReasonerPort | None = None


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
            try:
                import local_secrets
                value = getattr(local_secrets, name, None)
            except ImportError:
                pass
        return value or default

    main_model = env_or_local("AGENT2_LLM_MODEL", "deepseek-v4.1-flash")
    timeout_seconds = float(env.get("AGENT2_LLM_TIMEOUT_SECONDS", "30"))
    max_retries = int(env.get("AGENT2_LLM_MAX_RETRIES", "0"))

    def build_model(model_name: str, extra_body_env_name: str) -> OpenAICompatibleChatModel:
        return OpenAICompatibleChatModel(
            model_name=model_name,
            api_key=api_key,
            base_url=base_url,
            timeout_seconds=timeout_seconds,
            max_retries=max_retries,
            structured_mode=_structured_mode(env, model_name),
            extra_body=_extra_body(env, model_name, extra_body_env_name),
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
    return LlmComponents(understanding=LlmUserUnderstanding(understanding_model),
                         writer=LlmAnswerWriter(writer_model),
                         reasoner=LlmConversationReasoner(reasoner_model))
