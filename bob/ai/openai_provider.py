"""OpenAI provider."""
import json
import logging

from openai import AsyncOpenAI

import config
from .base import AIProvider, AIResponse, ToolCall

logger = logging.getLogger("discord")


class OpenAIProvider(AIProvider):
    def __init__(self) -> None:
        self.client = AsyncOpenAI(api_key=config.OPENAI_API_KEY, max_retries=0)
        self.model = config.get_model()
        logger.info(f"OpenAIProvider initialized with model: {self.model}")

    async def chat(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        tool_choice: str = "auto",
        temperature: float = 0.75,
        max_tokens: int = 1024,
    ) -> AIResponse:
        return await self._complete(self.model, messages, tools, tool_choice, temperature, max_tokens)

    async def _complete(
        self,
        model: str,
        messages: list[dict],
        tools: list[dict] | None,
        tool_choice: str,
        temperature: float,
        max_tokens: int,
    ) -> AIResponse:
        kwargs: dict = dict(
            model=model,
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
        )
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = tool_choice

        response = await self.request_with_retries(
            lambda: self.client.chat.completions.create(**kwargs),
            label="OpenAI chat request",
        )
        choice = response.choices[0]

        tool_calls: list[ToolCall] = []
        if choice.message.tool_calls:
            for tc in choice.message.tool_calls:
                try:
                    args = json.loads(tc.function.arguments or "{}")
                except json.JSONDecodeError:
                    args = {}
                tool_calls.append(
                    ToolCall(
                        id=tc.id, name=tc.function.name, arguments=args,
                        extra=(getattr(tc, "model_extra", None) or {}).get("extra_content"),
                    )
                )

        return AIResponse(
            content=choice.message.content,
            tool_calls=tool_calls,
            finish_reason=choice.finish_reason or "stop",
            input_tokens=response.usage.prompt_tokens if response.usage else 0,
            output_tokens=response.usage.completion_tokens if response.usage else 0,
        )
