"""Suggest a bounded per-domain dialogue action; code validates every suggestion."""

import json

from agent2.orchestration.contracts import OrchestrationInput, TaskPlan

from .contracts import StructuredModel
from .prompts import PromptRenderer
from .schemas import ConversationDecisions

PROMPT_VERSION = "conversation-reasoner-1"


class LlmConversationReasoner:
    prompt_version = PROMPT_VERSION

    def __init__(self, model: StructuredModel, renderer: PromptRenderer | None = None):
        self.model = model
        self.model_version = model.model_name
        self.renderer = renderer or PromptRenderer()

    async def decide(self, request: OrchestrationInput, plan: TaskPlan) -> ConversationDecisions:
        prompt = self.renderer.render(
            "conversation_reasoner.jinja2",
            user_text=request.text,
            task_json=json.dumps([{
                "task_id": task.task_id, "domain": task.domain, "intent": task.intent,
                "rule_actions": [action.operation for action in task.actions],
            } for task in plan.tasks], ensure_ascii=False),
            pending_json=json.dumps(request.session_pending_questions, ensure_ascii=False),
            last_assistant=request.session_last_assistant_text[:500],
        )
        return await self.model.parse(prompt=prompt, output_type=ConversationDecisions,
                                      operation="conversation_reasoner")
