"""Alibaba Model Studio adapter through its OpenAI-compatible Chat API."""

import json
from typing import TypeVar

from openai import AsyncOpenAI
from pydantic import BaseModel

StructuredResult = TypeVar("StructuredResult", bound=BaseModel)


class OpenAICompatibleChatModel:
    def __init__(self, *, model_name: str, api_key: str, base_url: str,
                 timeout_seconds: float = 60, max_retries: int = 1,
                 structured_mode: str = "json_schema", extra_body: dict | None = None,
                 client_factory=AsyncOpenAI):
        if structured_mode not in {"json_object", "json_schema"}:
            raise ValueError("structured_mode must be json_object or json_schema")
        self.model_name = model_name
        self.structured_mode = structured_mode
        self.extra_body = extra_body or {}
        self.client_factory = client_factory
        self.client_options = {
            "api_key": api_key,
            "base_url": base_url,
            "timeout": timeout_seconds,
            "max_retries": max_retries,
        }

    async def parse(self, *, prompt: str, output_type: type[StructuredResult],
                    operation: str) -> StructuredResult:
        messages = [
            {"role": "system", "content": prompt},
            {"role": "user", "content": "Return one JSON object matching this JSON Schema exactly:\n"
             + json.dumps(output_type.model_json_schema(), ensure_ascii=False)},
        ]
        # The synchronous service runs each turn with asyncio.run(), so every turn has a
        # different event loop. Keep the async HTTP client inside the current loop instead
        # of reusing a client whose connection pool belongs to a closed previous loop.
        client = self.client_factory(**self.client_options)
        try:
            if self.structured_mode == "json_schema":
                response = await client.chat.completions.parse(
                    model=self.model_name, messages=messages, response_format=output_type,
                    extra_body=self.extra_body,
                )
                parsed = response.choices[0].message.parsed
                if parsed is None:
                    raise RuntimeError(f"{operation}: model returned no structured output")
                return parsed
            response = await client.chat.completions.create(
                model=self.model_name, messages=messages,
                response_format={"type": "json_object"}, extra_body=self.extra_body,
            )
            message = response.choices[0].message
            content = message.content
            # Thinking models (e.g. glm-5.3) may leave content empty and put the
            # final JSON only in reasoning_content when the budget is exhausted.
            if not content:
                content = getattr(message, "reasoning_content", None) or ""
            if not content:
                raise RuntimeError(f"{operation}: model returned empty JSON")
            return output_type.model_validate_json(content)
        finally:
            await client.close()
