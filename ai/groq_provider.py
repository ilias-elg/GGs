"""Groq provider — uses the OpenAI-compatible API with the AsyncOpenAI client."""
import json
import logging

from openai import AsyncOpenAI

import config
from .base import AIProvider, AIResponse, ToolCall

logger = logging.getLogger("discord")

_GROQ_BASE_URL = "https://api.groq.com/openai/v1"


class GroqProvider(AIProvider):
    def __init__(self) -> None:
        self.client = AsyncOpenAI(
            base_url=_GROQ_BASE_URL,
            api_key=config.GROQ_API_KEY,
            max_retries=0,
        )
        self.model = config.get_model()
        logger.info(f"GroqProvider initialized with model: {self.model}")

    async def chat(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        tool_choice: str = "auto",
        temperature: float = 0.75,
        max_tokens: int = 1024,
    ) -> AIResponse:
        kwargs: dict = dict(
            model=self.model,
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
        )
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = tool_choice

        try:
            response = await self.request_with_retries(
                lambda: self.client.chat.completions.create(**kwargs),
                label="Groq chat request",
            )
        except Exception as exc:
            if getattr(exc, "status_code", None) == 404 and "model" in str(exc).lower():
                logger.warning(f"Model {self.model} not found. Attempting to fallback...")
                try:
                    models = await self.client.models.list()
                    valid = [m.id for m in models.data if "whisper" not in m.id.lower() and "guard" not in m.id.lower() and "orpheus" not in m.id.lower()]
                    if valid:
                        fallback = valid[0]
                        # Prefer larger capable models for chat
                        for pref in ["groq/compound", "gpt-oss-120b", "qwen"]:
                            if any(pref in m for m in valid):
                                fallback = next(m for m in valid if pref in m)
                                break
                        logger.info(f"Fallback to model {fallback}")
                        self.model = fallback
                        kwargs["model"] = fallback
                        response = await self.request_with_retries(
                            lambda: self.client.chat.completions.create(**kwargs),
                            label="Groq chat request fallback",
                        )
                    else:
                        raise exc
                except Exception as inner_exc:
                    if inner_exc is not exc:
                        logger.error(f"Fallback failed: {inner_exc}")
                    raise exc
            else:
                raise

        choice = response.choices[0]

        tool_calls: list[ToolCall] = []
        if choice.message.tool_calls:
            for tc in choice.message.tool_calls:
                try:
                    args = json.loads(tc.function.arguments or "{}")
                except json.JSONDecodeError:
                    args = {}
                    logger.warning(f"Failed to parse tool args for {tc.function.name}: {tc.function.arguments}")
                tool_calls.append(
                    ToolCall(id=tc.id, name=tc.function.name, arguments=args)
                )

        return AIResponse(
            content=choice.message.content,
            tool_calls=tool_calls,
            finish_reason=choice.finish_reason or "stop",
            input_tokens=response.usage.prompt_tokens if response.usage else 0,
            output_tokens=response.usage.completion_tokens if response.usage else 0,
        )
