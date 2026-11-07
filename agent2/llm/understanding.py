"""Free-text understanding. Candidate facts are validated outside this adapter."""

import json

from agent2.orchestration.contracts import OrchestrationInput

from .contracts import StructuredModel
from .prompts import PromptRenderer
from .schemas import UnderstandingResult

PROMPT_VERSION = "understand-user-v5"


class LlmUserUnderstanding:
    prompt_version = PROMPT_VERSION

    def __init__(self, model: StructuredModel, renderer: PromptRenderer | None = None):
        self.model = model
        self.model_version = model.model_name
        self.renderer = renderer or PromptRenderer()

    async def understand(self, request: OrchestrationInput,
                         allowed_fields: dict[str, dict]) -> UnderstandingResult:
        prompt = self.renderer.render(
            "understand_user.jinja2",
            user_text=request.text,
            allowed_fields_json=json.dumps(allowed_fields, ensure_ascii=False, sort_keys=True),
            session_intents_json=json.dumps(list(request.conversation_intents), ensure_ascii=False),
            session_topic=request.session_topic or "",
            session_last_user_text=request.session_last_user_text,
            session_last_assistant_text=request.session_last_assistant_text,
            session_pending_questions_json=json.dumps(list(request.session_pending_questions),
                                                      ensure_ascii=False),
        )
        return await self.model.parse(prompt=prompt, output_type=UnderstandingResult,
                                      operation="understand_user")
