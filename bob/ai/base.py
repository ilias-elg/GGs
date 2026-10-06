"""Abstract AI provider interface."""
import asyncio
import logging
import random
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

logger = logging.getLogger("discord")


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]
    # Provider data that has to be sent back unchanged with this call on the
    # next round — Gemini's thought signature, without which it rejects the
    # whole request.
    extra: dict[str, Any] | None = None


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

    async def request_with_retries(
        self,
        operation: Callable[[], Awaitable[Any]],
        label: str = "AI request",
        max_retries: int = 3,
    ) -> Any:
        """Run an API request with bounded 429/5xx backoff.

        Groq exposes ``retry-after`` and reset headers on rate-limit responses;
        when present, those are preferred over exponential backoff. This keeps
        tool loops from failing on a short RPM/TPM spike.
        """
        for attempt in range(max_retries + 1):
            try:
                return await operation()
            except Exception as exc:
                status = getattr(exc, "status_code", None)
                if status not in {429, 500, 502, 503, 504} or attempt >= max_retries:
                    raise

                delay = self._retry_delay(exc, attempt)
                logger.warning(
                    "%s received HTTP %s; retrying in %.1fs (%s/%s)",
                    label,
                    status,
                    delay,
                    attempt + 1,
                    max_retries,
                )
                await asyncio.sleep(delay)

    @staticmethod
    def _retry_delay(exc: Exception, attempt: int) -> float:
        headers = getattr(getattr(exc, "response", None), "headers", None) or getattr(exc, "headers", None) or {}
        raw = headers.get("retry-after") or headers.get("Retry-After")
        if raw:
            match = re.search(r"\d+(?:\.\d+)?", str(raw))
            if match:
                return min(max(float(match.group(0)), 0.5), 60.0)
        # Small jitter prevents multiple channels retrying simultaneously.
        return min((2 ** attempt) + random.uniform(0.1, 0.8), 30.0)

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
