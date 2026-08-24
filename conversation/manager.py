"""
ConversationManager — the agentic core of Bob.

Full pipeline per message:
  1. Build token-efficient context (history + memories + Discord metadata).
  2. Call AI with all tool schemas.
  3. Execute tool calls — up to MAX_TOOL_ROUNDS rounds.
  4. Send final response to Discord (splitting if > 2000 chars).
  5. Background: extract memories, optionally summarise channel.

Key improvements over the original:
  - Per-channel asyncio.Lock prevents race conditions in busy channels.
  - Up to 10 tool rounds (was 3).
  - Full async — no run_in_executor for AI calls.
  - Proper assistant message reconstruction for multi-round tool loops.
  - Long responses split at natural newline boundaries.
  - show_memories / forget are now handled naturally by the AI.
"""

import json
import asyncio
import logging
from collections import defaultdict

import discord

import config
from ai.base import AIProvider, AIResponse, ToolCall
from conversation import memory as mem
from conversation.context import build_context
from tools import get_tools_for_context, execute_tool

logger = logging.getLogger("discord")

# ---------------------------------------------------------------------------
# Background AI provider (cheap/fast model for memory extraction + summaries)
# ---------------------------------------------------------------------------

_bg_provider: AIProvider | None = None

# These tools can create irreversible or externally visible side effects.
# The model may propose them, but the manager—not the model—owns confirmation.
_CONFIRMATION_TOOLS = frozenset({
    "create_channel", "delete_channel", "rename_channel", "edit_channel",
    "create_category", "set_channel_permissions", "create_role", "delete_role",
    "edit_role", "kick_member", "ban_member", "unban_member", "timeout_member",
    "remove_timeout", "assign_role", "remove_role", "change_nickname",
    "send_message", "delete_message", "bulk_delete_messages", "pin_message",
    "run_local_task", "write_workspace_file", "delete_workspace_file",
})
_YES_WORDS = frozenset({"yes", "y", "yeah", "yep", "sure", "confirm", "confirmed", "do it", "go ahead", "proceed"})
_NO_WORDS = frozenset({"no", "n", "nope", "cancel", "stop", "don't", "do not"})


def _get_bg_provider() -> AIProvider:
    """
    Return a lazy-initialized background AI provider using the cheap model.
    Groq: llama-3.1-8b-instant — very fast, uses almost no quota.
    """
    global _bg_provider
    if _bg_provider is None:
        try:
            # Reuse the configured provider so OpenAI and Anthropic installs
            # do not unexpectedly attempt a Groq request in the background.
            from ai import create_provider
            _bg_provider = create_provider()
            _bg_provider.model = config.get_background_model()
        except Exception:
            # If background provider fails, fall back to None (caller will skip)
            pass
    return _bg_provider



# ---------------------------------------------------------------------------
# Background helpers
# ---------------------------------------------------------------------------


async def _extract_memories(
    ai: AIProvider, user_id: int, username: str, guild_id: int | None, snippet: str
) -> None:
    """
    Ask the AI whether anything in the last exchange is worth remembering.
    Uses the cheap background model to preserve main quota.
    Runs in the background after each response is sent.
    """
    bg = _get_bg_provider() or ai  # fall back to main provider if bg fails
    try:
        raw = await bg.simple_complete(
            system=(
                "Extract memories from this conversation. "
                "Return ONLY JSON: "
                '{\"user_memory\": \"...\", \"server_memory\": \"...\", \"importance\": 1-10}\n'
                "user_memory: fact about this specific user worth remembering long-term.\n"
                "server_memory: server/project fact worth remembering (decisions, setups, plans).\n"
                "importance: 1-10. Set a field to null if nothing worth saving.\n"
                "Do NOT save: greetings, small talk, live Roblox counts, one-off commands."
            ),
            user=snippet,
            max_tokens=150,
            temperature=0.1,
        )

        # Strip markdown fences if present
        raw = raw.strip()
        if raw.startswith("```"):
            lines = raw.split("\n")
            raw = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])

        data = json.loads(raw)
        importance = max(1, min(10, int(data.get("importance", 5))))

        if data.get("user_memory"):
            await mem.save_user_memory(user_id, data["user_memory"], importance)
            logger.info(f"Saved user memory for {username}: {data['user_memory']}")

        if data.get("server_memory") and guild_id:
            await mem.save_server_memory(guild_id, data["server_memory"], importance)
            logger.info(f"Saved server memory for guild {guild_id}: {data['server_memory']}")

    except Exception as e:
        logger.debug(f"Memory extraction skipped: {e}")


async def _maybe_summarise(ai: AIProvider, channel_id: int) -> None:
    """
    If channel history is near the cap, generate a compressed summary
    of the oldest messages and save it. Uses the cheap background model.
    """
    history = mem.get_history(channel_id)
    if len(history) < 18:
        return

    bg = _get_bg_provider() or ai
    text = "\n".join(
        f"{h.get('username', h['role'])}: {h['content']}"
        for h in history[:10]  # Summarize the oldest 10
    )
    try:
        summary = await bg.simple_complete(
            system=(
                "Summarize this Discord conversation in 2-3 sentences. "
                "Key topics, decisions, relevant facts only. Be concise."
            ),
            user=text,
            max_tokens=120,
            temperature=0.2,
        )
        if summary:
            await mem.save_channel_summary(channel_id, summary)
            logger.info(f"Saved channel summary for {channel_id}")
    except Exception as e:
        logger.debug(f"Summarization skipped: {e}")


# ---------------------------------------------------------------------------
# Message formatting
# ---------------------------------------------------------------------------


def _build_assistant_message(response: AIResponse) -> dict:
    """Convert an AIResponse back to OpenAI-format assistant message for next round."""
    msg: dict = {"role": "assistant", "content": response.content or ""}
    if response.tool_calls:
        msg["tool_calls"] = [
            {
                "id": tc.id,
                "type": "function",
                "function": {
                    "name": tc.name,
                    "arguments": json.dumps(tc.arguments, ensure_ascii=False),
                },
            }
            for tc in response.tool_calls
        ]
    return msg


async def _send_response(message: discord.Message, text: str) -> None:
    """Send a response, splitting at 2000 chars on natural newline boundaries."""
    if not text:
        return

    if len(text) <= 1990:
        await message.reply(text)
        return

    # Split on newlines, reassembling into ≤1990-char chunks
    chunks: list[str] = []
    current = ""
    for line in text.split("\n"):
        candidate = (current + "\n" + line).lstrip() if current else line
        if len(candidate) > 1990:
            if current:
                chunks.append(current)
            # If a single line itself is too long, hard-split it
            while len(line) > 1990:
                chunks.append(line[:1990])
                line = line[1990:]
            current = line
        else:
            current = candidate
    if current:
        chunks.append(current)

    for i, chunk in enumerate(chunks):
        if i == 0:
            await message.reply(chunk)
        else:
            await message.channel.send(chunk)


# ---------------------------------------------------------------------------
# ConversationManager
# ---------------------------------------------------------------------------


class ConversationManager:
    def __init__(self, ai_provider: AIProvider, bot, voice_manager=None) -> None:
        self.ai = ai_provider
        self.bot = bot
        self.voice_manager = voice_manager
        # Per-channel lock ensures messages in the same channel are processed
        # one at a time, preventing context corruption from concurrent requests.
        self._locks: dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._pending_confirmations: dict[tuple[int, int], list[ToolCall]] = {}

    @staticmethod
    def _is_confirmation(content: str, words: frozenset[str]) -> bool:
        normalized = " ".join(content.lower().strip().split())
        return normalized in words or any(normalized.startswith(f"{word} ") for word in words if " " in word)

    @staticmethod
    def _describe_pending(calls: list[ToolCall]) -> str:
        descriptions = []
        for call in calls:
            if call.name == "run_local_task":
                descriptions.append(f"run `{call.arguments.get('command', '')}` locally")
            else:
                args = ", ".join(f"{key}={value}" for key, value in call.arguments.items() if key != "reason")
                descriptions.append(f"{call.name.replace('_', ' ')}" + (f" ({args})" if args else ""))
        return "; ".join(descriptions)

    async def _execute_confirmed(
        self, message: discord.Message, calls: list[ToolCall], tool_ctx: dict
    ) -> None:
        results = []
        for call in calls:
            result = await execute_tool(call.name, call.arguments, tool_ctx)
            if result.get("success") or result.get("sent"):
                results.append(f"{call.name.replace('_', ' ')}: {result.get('result', 'completed')}")
            else:
                results.append(f"{call.name.replace('_', ' ')}: failed — {result.get('error', 'unknown error')}")
        answer = "Done — " + "\n".join(results)
        await _send_response(message, answer)
        mem.add_to_history(message.channel.id, "assistant", answer)
        mem.activate_conversation(message.channel.id, message.author.id)

    async def handle(self, message: discord.Message, content: str) -> None:
        """Entry point — acquires per-channel lock, then runs the agent loop."""
        channel_id = message.channel.id
        async with self._locks[channel_id]:
            key = (channel_id, message.author.id)
            pending = self._pending_confirmations.get(key)
            if pending:
                if self._is_confirmation(content, _YES_WORDS):
                    self._pending_confirmations.pop(key, None)
                    tool_ctx = {
                        "message": message,
                        "bot": self.bot,
                        "voice_manager": self.voice_manager,
                        "channel_id": channel_id,
                    }
                    async with message.channel.typing():
                        await self._execute_confirmed(message, pending, tool_ctx)
                    return
                if self._is_confirmation(content, _NO_WORDS):
                    self._pending_confirmations.pop(key, None)
                    await message.reply("Canceled — I didn’t change anything.")
                    return
                # A new request supersedes an unanswered confirmation.
                self._pending_confirmations.pop(key, None)
            await self._agent_loop(message, content)

    async def _agent_loop(self, message: discord.Message, content: str) -> None:
        """Full agentic pipeline: context → AI → tools → AI → ... → respond."""
        channel_id = message.channel.id
        user_id = message.author.id
        username = message.author.display_name
        guild_id = message.guild.id if message.guild else None

        # Build context
        ctx_data = await build_context(message, content, self.ai)
        messages: list[dict] = ctx_data["messages"]
        in_guild: bool = ctx_data["in_guild"]

        # Tool schemas — intent-based selection saves ~2,000 tokens per request
        has_image = bool(
            message.attachments and
            any(a.content_type and a.content_type.startswith("image/") for a in message.attachments)
        )
        tool_schemas = get_tools_for_context(content, in_guild=in_guild, has_image=has_image)
        logger.debug(f"Selected {len(tool_schemas)} tools for: {content[:60]!r}")

        # Tool execution context passed to discord_tools
        tool_ctx = {
            "message": message,
            "bot": self.bot,
            "voice_manager": self.voice_manager,
            "channel_id": channel_id,
        }

        async with message.channel.typing():
            try:
                # ── First AI call ────────────────────────────────────────────
                response = await self.ai.chat(
                    messages=messages,
                    tools=tool_schemas,
                    tool_choice="auto",
                    temperature=0.75,
                    max_tokens=1024,
                )

                # ── Agentic tool loop ────────────────────────────────────────
                rounds = 0
                dashboard_sent = False
                while response.has_tool_calls and rounds < config.MAX_TOOL_ROUNDS:
                    rounds += 1
                    logger.info(
                        f"Tool round {rounds}: calling {[tc.name for tc in response.tool_calls]}"
                    )

                    # Execute all tool calls in this round
                    tool_results: list[dict] = []
                    for tc in response.tool_calls:
                        if tc.name in _CONFIRMATION_TOOLS:
                            key = (channel_id, user_id)
                            self._pending_confirmations[key] = list(response.tool_calls)
                            prompt = self._describe_pending(response.tool_calls)
                            await message.reply(
                                f"I’m ready to {prompt}. This will make a real change. "
                                "Reply **yes** to confirm or **no** to cancel."
                            )
                            mem.add_to_history(channel_id, "assistant", f"Confirmation requested: {prompt}")
                            return
                        logger.info(f"Executing tool: {tc.name} args={tc.arguments}")
                        result = await execute_tool(tc.name, tc.arguments, tool_ctx)
                        logger.info(f"Tool result [{tc.name}]: {json.dumps(result)[:200]}")

                        if tc.name == "send_dashboard" and result.get("sent"):
                            dashboard_sent = True

                        tool_results.append({
                            "role": "tool",
                            "tool_call_id": tc.id,
                            "content": json.dumps(result, ensure_ascii=False),
                        })

                    # send_dashboard already delivered the user-visible
                    # response. A second model call adds latency and can hit
                    # Groq's RPM/TPM limit for no benefit.
                    if dashboard_sent and all(
                        tc.name == "send_dashboard" for tc in response.tool_calls
                    ):
                        response = AIResponse(content=None)
                        break

                    # Append assistant's tool call message + results
                    messages.append(_build_assistant_message(response))
                    messages.extend(tool_results)

                    # Next AI call to evaluate results and decide next action
                    response = await self.ai.chat(
                        messages=messages,
                        tools=tool_schemas,
                        tool_choice="auto",
                        temperature=0.75,
                        max_tokens=1024,
                    )

                # ── Extract final text ───────────────────────────────────────
                answer = response.content or ""

                # If the dashboard embed was already sent, don't also send a text wall
                if dashboard_sent:
                    answer = ""
                elif not answer.strip():
                    answer = "Hmm, got nothing back on that one. Try again?"

                # ── Send response ────────────────────────────────────────────
                if answer.strip():
                    await _send_response(message, answer)
                    mem.activate_conversation(channel_id, user_id)

                # ── Update history ───────────────────────────────────────────
                mem.add_to_history(channel_id, "assistant", answer)

                # ── Background tasks (non-blocking) ─────────────────────────
                snippet = f"{username}: {content}\nBob: {answer}"
                asyncio.create_task(
                    _extract_memories(self.ai, user_id, username, guild_id, snippet)
                )
                asyncio.create_task(_maybe_summarise(self.ai, channel_id))

            except Exception as e:
                logger.error(f"ConversationManager error: {e}", exc_info=True)
                try:
                    if getattr(e, "status_code", None) == 429:
                        await message.reply(
                            "Groq is rate-limiting requests right now. "
                            "I already retried with backoff; please try again in a few seconds."
                        )
                    else:
                        await message.reply(f"Something went wrong on my end: {e}")
                except Exception:
                    pass
