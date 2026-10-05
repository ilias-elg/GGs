"""Gemini provider — Google AI Studio's OpenAI-compatible endpoint (what Jarvis used)."""
import logging
import re
import time

from openai import AsyncOpenAI

import config
from .base import AIResponse
from .openai_provider import OpenAIProvider

logger = logging.getLogger("discord")

_GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai"

# Model → monotonic time it can be used again. Shared by every provider
# instance (chat and background), since the quota is per API key and model.
_unavailable_until: dict[str, float] = {}


class AIQuotaExhausted(Exception):
    """Every configured model is out of quota. `user_message` is safe to show in chat."""

    status_code = 429

    def __init__(self, wait_seconds: float) -> None:
        hours, minutes = divmod(int(wait_seconds) // 60, 60)
        when = f"{hours}h {minutes}m" if hours else f"{max(minutes, 1)}m"
        self.user_message = (
            f"I'm out of Google AI quota on every model I can use — it resets in about {when}. "
            "Slash commands still work in the meantime."
        )
        super().__init__(self.user_message)


class _DailyQuota(Exception):
    """A 429 for a per-day quota: waiting a few seconds and retrying cannot help."""

    def __init__(self, wait_seconds: float) -> None:
        self.wait_seconds = wait_seconds
        super().__init__(f"daily quota exhausted for {wait_seconds:.0f}s")


def _daily_quota_wait(exc: Exception) -> float | None:
    """Seconds until a daily quota resets, or None if `exc` isn't a daily-quota 429."""
    if getattr(exc, "status_code", None) != 429:
        return None
    text = str(exc)
    delay = re.search(r"retryDelay['\"]?: ?['\"]?(\d+(?:\.\d+)?)s", text)
    wait = float(delay.group(1)) if delay else None
    if "PerDay" in text or (wait is not None and wait > 300):
        return wait or 3600.0
    return None


class GeminiProvider(OpenAIProvider):
    def __init__(self) -> None:
        self.client = AsyncOpenAI(
            base_url=_GEMINI_BASE_URL,
            api_key=config.GOOGLE_API_KEY,
            max_retries=0,
        )
        self.model = config.get_model()
        logger.info(
            f"GeminiProvider initialized with model: {self.model} "
            f"(fallbacks: {', '.join(config.GEMINI_FALLBACK_MODELS) or 'none'})"
        )

    async def request_with_retries(self, operation, label="AI request", max_retries=3):
        # Short per-minute limits and 5xx errors are worth the usual backoff;
        # a spent daily quota is not, so it is surfaced straight away.
        async def guarded():
            try:
                return await operation()
            except Exception as exc:
                wait = _daily_quota_wait(exc)
                if wait is not None:
                    raise _DailyQuota(wait) from exc
                raise

        # With other models to fall back on, one retry is enough: when Google
        # reports a model as overloaded, the next model answers sooner than
        # this one recovers.
        if config.GEMINI_FALLBACK_MODELS:
            max_retries = min(max_retries, 1)
        return await super().request_with_retries(guarded, label, max_retries)

    async def chat(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        tool_choice: str = "auto",
        temperature: float = 0.75,
        max_tokens: int = 1024,
    ) -> AIResponse:
        """
        Chat on the configured model, moving down GEMINI_FALLBACK_MODELS when
        one runs out of its daily free-tier quota (each model has its own), is
        retired, or keeps failing.
        """
        candidates = list(dict.fromkeys([self.model, *config.GEMINI_FALLBACK_MODELS]))
        last_error: Exception | None = None
        for model in candidates:
            if _unavailable_until.get(model, 0) > time.monotonic():
                continue
            try:
                return await self._complete(model, messages, tools, tool_choice, temperature, max_tokens)
            except _DailyQuota as exc:
                _unavailable_until[model] = time.monotonic() + exc.wait_seconds
                logger.warning(
                    f"Gemini model {model} is out of daily quota for {exc.wait_seconds / 3600:.1f}h; trying the next model."
                )
            except Exception as exc:
                status = getattr(exc, "status_code", None)
                if status == 404:
                    # Retired or not available to this key.
                    _unavailable_until[model] = time.monotonic() + 24 * 3600
                elif status in {429, 500, 502, 503, 504}:
                    _unavailable_until[model] = time.monotonic() + 60
                else:
                    raise
                last_error = exc
                logger.warning(f"Gemini model {model} failed with HTTP {status}; trying the next model.")

        waits = [until - time.monotonic() for m, until in _unavailable_until.items() if m in candidates]
        if last_error is not None and (not waits or min(waits) <= 60):
            raise last_error
        raise AIQuotaExhausted(max(min(waits), 60) if waits else 3600)
