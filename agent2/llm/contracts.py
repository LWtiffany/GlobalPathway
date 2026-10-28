"""Provider-neutral LLM contracts used by Agent2 orchestration."""

from typing import Protocol, TypeVar

from pydantic import BaseModel

from agent2.models import AnswerBody
from agent2.orchestration.contracts import OrchestrationInput, UnifiedAnswer

from .schemas import UnderstandingResult

StructuredResult = TypeVar("StructuredResult", bound=BaseModel)


class StructuredModel(Protocol):
    model_name: str

    async def parse(self, *, prompt: str, output_type: type[StructuredResult],
                    operation: str) -> StructuredResult: ...


class UserUnderstandingPort(Protocol):
    model_version: str
    prompt_version: str

    async def understand(self, request: OrchestrationInput,
                         allowed_fields: dict[str, dict]) -> UnderstandingResult: ...


class AnswerWriterPort(Protocol):
    model_version: str
    prompt_version: str

    async def write(self, *, request: OrchestrationInput,
                    verified_answer: UnifiedAnswer) -> AnswerBody: ...
