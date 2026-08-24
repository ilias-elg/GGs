"""
Anthropic provider.

Handles the format differences between OpenAI and Anthropic APIs:
- System prompt is a separate parameter (not a message with role=system)
- Tool definitions use a different schema (input_schema instead of parameters)
- Tool calls in assistant messages use a different structure
- Tool results are embedded in user messages with a special content type
"""
import json
import logging

import config
from .base import AIProvider, AIResponse, ToolCall

logger = logging.getLogger("discord")


class AnthropicProvider(AIProvider):
    def __init__(self) -> None:
        try:
            import anthropic as _sdk
            self._sdk = _sdk
        except ImportError:
            raise ImportError(
                "anthropic package not installed. Run: pip install anthropic"
            )
        self.client = self._sdk.AsyncAnthropic(api_key=config.ANTHROPIC_API_KEY, max_retries=0)
        self.model = config.get_model()
        logger.info(f"AnthropicProvider initialized with model: {self.model}")

    def _convert_tools(self, tools: list[dict]) -> list[dict]:
        """Convert OpenAI tool format → Anthropic tool format."""
        result = []
        for tool in tools:
            fn = tool.get("function", {})
            result.append({
                "name": fn["name"],
                "description": fn.get("description", ""),
                "input_schema": fn.get("parameters", {"type": "object", "properties": {}}),
            })
        return result

    def _extract_system_and_messages(
        self, messages: list[dict]
    ) -> tuple[str, list[dict]]:
        """
        Split OpenAI messages into (system_string, anthropic_messages).
        Tool results and tool calls are also converted to Anthropic format.
        """
        system_parts: list[str] = []
        anthropic_msgs: list[dict] = []

        for msg in messages:
            role = msg.get("role")
            content = msg.get("content")

            if role == "system":
                system_parts.append(content or "")

            elif role == "tool":
                # Tool result → embed in a user message as tool_result content block
                anthropic_msgs.append({
                    "role": "user",
                    "content": [{
                        "type": "tool_result",
                        "tool_use_id": msg["tool_call_id"],
                        "content": content or "",
                    }],
                })

            elif role == "assistant":
                blocks: list[dict] = []
                if content:
                    blocks.append({"type": "text", "text": content})
                for tc in msg.get("tool_calls", []):
                    fn = tc.get("function", {})
                    try:
                        inp = json.loads(fn.get("arguments") or "{}")
                    except json.JSONDecodeError:
                        inp = {}
                    blocks.append({
                        "type": "tool_use",
                        "id": tc["id"],
                        "name": fn["name"],
                        "input": inp,
                    })
                if blocks:
                    anthropic_msgs.append({"role": "assistant", "content": blocks})

            elif role == "user":
                anthropic_msgs.append({"role": "user", "content": content or ""})

        return "\n".join(system_parts).strip(), anthropic_msgs

    async def chat(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        tool_choice: str = "auto",
        temperature: float = 0.75,
        max_tokens: int = 1024,
    ) -> AIResponse:
        system, anthropic_msgs = self._extract_system_and_messages(messages)

        kwargs: dict = dict(
            model=self.model,
            max_tokens=max_tokens,
            temperature=temperature,
            messages=anthropic_msgs,
        )
        if system:
            kwargs["system"] = system
        if tools:
            kwargs["tools"] = self._convert_tools(tools)
            kwargs["tool_choice"] = {"type": "auto"} if tool_choice == "auto" else {"type": "any"}

        response = await self.request_with_retries(
            lambda: self.client.messages.create(**kwargs),
            label="Anthropic chat request",
        )

        content_text: str | None = None
        tool_calls: list[ToolCall] = []

        for block in response.content:
            if block.type == "text":
                content_text = block.text
            elif block.type == "tool_use":
                tool_calls.append(
                    ToolCall(id=block.id, name=block.name, arguments=block.input or {})
                )

        return AIResponse(
            content=content_text,
            tool_calls=tool_calls,
            finish_reason="tool_calls" if tool_calls else "stop",
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
        )
