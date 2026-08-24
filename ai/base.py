"""Abstract AI provider interface."""
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class AIResponse:
    content: str | None
    tool_calls: list[ToolCall] = field(default_factory=list)
    finish_reason: str = "stop"
    input_tokens: int = 0
    output_tokens: int = 0

    @property
    def has_tool_calls(self) -> bool:
        return bool(self.tool_calls)


class AIProvider(ABC):
    """
    Abstract base for all AI providers.

    Implementations must accept OpenAI-format messages and tool schemas,
    and return a normalized AIResponse. This allows swapping providers
    by changing AI_PROVIDER in .env without touching other code.
    """

    @abstractmethod
    async def chat(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        tool_choice: str = "auto",
        temperature: float = 0.75,
        max_tokens: int = 1024,
    ) -> AIResponse:
        """Send a chat request and return a normalized response."""
        ...

    async def simple_complete(
        self,
        system: str,
        user: str,
        max_tokens: int = 300,
        temperature: float = 0.2,
    ) -> str:
        """
        Lightweight one-shot completion for background tasks
        (memory extraction, summarization). No tools, low temperature.
        """
        resp = await self.chat(
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            tools=None,
            temperature=temperature,
            max_tokens=max_tokens,
        )
        return resp.content or ""
