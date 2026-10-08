"""Gemini provider — Google AI Studio's OpenAI-compatible endpoint (what Jarvis used)."""
import logging
import re
import time

from openai import APIConnectionError, APITimeoutError, AsyncOpenAI

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


class AIBusy(Exception):
    """Every model was busy or slow just now. `user_message` is safe to show in chat."""

    status_code = 503
    user_message = "Google's AI is overloaded right now and none of my models answered. Try again in a minute."


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


# How long one model gets to answer before the next one is tried. Measured on
# the free tier: a healthy model answers in 1-5 seconds, an overloaded one can
# sit for half a minute.
REQUEST_TIMEOUT_SECONDS = 20.0
# How long a model is left alone after it was overloaded or timed out.
BUSY_COOLDOWN_SECONDS = 30.0
# After this long spent on models that failed, no further model is started:
# five slow models in a row would otherwise keep someone waiting 100 seconds.
MAX_TOTAL_SECONDS = 45.0


def _short_wait(exc: Exception) -> float:
    """Seconds a per-minute 429 asks us to wait, capped — the next model is used meanwhile."""
    delay = re.search(r"retryDelay['\"]?: ?['\"]?(\d+(?:\.\d+)?)s", str(exc))
    return min(max(float(delay.group(1)), 5.0), 60.0) if delay else BUSY_COOLDOWN_SECONDS


class GeminiProvider(OpenAIProvider):
    def __init__(self) -> None:
        self.client = AsyncOpenAI(
            base_url=_GEMINI_BASE_URL,
            api_key=config.GOOGLE_API_KEY,
            max_retries=0,
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        self.model = config.get_model()
        # Tried in order after self.model. Background work narrows this to
        # the lite models so it never spends the chat models' quota.
        self.fallbacks: list[str] = list(config.GEMINI_FALLBACK_MODELS)
        logger.info(
            f"GeminiProvider initialized with model: {self.model} "
            f"(fallbacks: {', '.join(self.fallbacks) or 'none'})"
        )

    async def request_with_retries(self, operation, label="AI request", max_retries=3):
        # A spent daily quota can't be waited out, so it is surfaced at once.
        async def guarded():
            try:
                return await operation()
            except Exception as exc:
                wait = _daily_quota_wait(exc)
                if wait is not None:
                    raise _DailyQuota(wait) from exc
                raise

        # With other models to fall back on, waiting and retrying the same
        # one is the slow option: a busy model stays busy for a while, and the
        # next model usually answers within a second or two.
        if self.fallbacks:
            max_retries = 0
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
        Chat on the configured model, moving straight down the fallbacks when
        one is out of quota, rate-limited, overloaded, slow or retired.
        """
        candidates = list(dict.fromkeys([self.model, *self.fallbacks]))
        now = time.monotonic()
        ready = [m for m in candidates if _unavailable_until.get(m, 0) <= now]
        if not ready:
            # Everything is cooling down. A short cool-down is only a guess
            # that the model is still busy, so the soonest one is tried
            # anyway rather than failing the reply outright.
            soonest = min(candidates, key=lambda m: _unavailable_until.get(m, 0))
            if _unavailable_until[soonest] - now <= 2 * 60:
                ready = [soonest]

        last_error: Exception | None = None
        for model in ready:
            if last_error is not None and time.monotonic() - now > MAX_TOTAL_SECONDS:
                break
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
                    cooldown, why = 24 * 3600, "is not available"
                elif status == 429:
                    cooldown, why = _short_wait(exc), "is rate-limited"
                elif status in {500, 502, 503, 504}:
                    cooldown, why = BUSY_COOLDOWN_SECONDS, f"is overloaded (HTTP {status})"
                elif isinstance(exc, (APITimeoutError, APIConnectionError)):
                    cooldown, why = BUSY_COOLDOWN_SECONDS, "did not answer in time"
                else:
                    raise
                _unavailable_until[model] = time.monotonic() + cooldown
                last_error = exc
                logger.warning(f"Gemini model {model} {why}; trying the next model.")

        waits = [until - time.monotonic() for m, until in _unavailable_until.items() if m in candidates]
        if last_error is not None and (not waits or min(waits) <= 2 * 60):
            raise AIBusy() from last_error
        raise AIQuotaExhausted(max(min(waits), 60) if waits else 3600)
