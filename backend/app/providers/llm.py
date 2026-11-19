"""The LLM capability: a vendor-neutral request/response and the adapter base class.

Business logic builds an :class:`LLMRequest` and asks the ProviderManager for the ``LLM`` capability; it never
imports a vendor. Prompts live in ``app/prompts/`` (not here, not in services). Anything that came from the outside
world (a prospect's reply, a scraped page) is **data**, passed in ``user`` and never mixed into ``system``.
"""

from __future__ import annotations

from abc import abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

from app.providers.registry import Provider
from app.providers.spec import ProviderResult

ReasoningEffort = Literal["low", "medium", "high"]


@dataclass(frozen=True, slots=True)
class LLMRequest:
    system: str
    user: str
    # A JSON Schema. When set, the model is constrained to it (strict structured output) and ``parsed`` is filled.
    json_schema: Mapping[str, Any] | None = None
    schema_name: str = "response"
    max_tokens: int = 1024  # ceiling for the completion, including any hidden reasoning tokens
    temperature: float = 0.2
    reasoning_effort: ReasoningEffort = "low"


@dataclass(frozen=True, slots=True)
class LLMResponse:
    text: str
    parsed: Any | None  # the decoded JSON value when a schema was requested
    model: str
    prompt_tokens: int
    completion_tokens: int
    finish_reason: str


class LLMProvider(Provider):
    @abstractmethod
    async def complete(self, request: LLMRequest) -> ProviderResult[LLMResponse]:
        """One chat completion. Raises ``app.providers.errors`` types only."""


def estimate_tokens(request: LLMRequest) -> int:
    """Rough upper bound used for per-minute pacing (~3.5 characters per token + the completion ceiling)."""
    prompt = len(request.system) + len(request.user)
    return int(prompt / 3.5) + request.max_tokens
