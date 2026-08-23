"""
ConversationManager — the brain of Bob.

Pipeline per message:
  1. Determine if bot should respond.
  2. Build context (history + user memories + channel summary).
  3. Call Groq with Roblox tool definitions.
  4. Execute any tool calls, feed results back.
  5. Send final response to Discord.
  6. Background: extract memories, optionally summarise channel.
"""

import json
import asyncio
import logging
import discord
from openai import OpenAI

from . import memory as mem
from .tools import TOOL_SCHEMAS, execute_tool

logger = logging.getLogger("discord")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

AI_MODEL = "openai/gpt-oss-120b"
AI_TEMPERATURE = 0.75
AI_MAX_TOKENS = 1024
AI_CHANNEL_ID = 1090066231312261133   # responds to EVERY message here

# How many messages from history to include in the prompt
HISTORY_IN_PROMPT = 15

SYSTEM_PROMPT = """You are Bob — a sharp, casual, and genuinely intelligent Discord bot built for a Roblox group intelligence server.

You monitor three Roblox communities in real-time:
• TSB Air  (Group ID 485588074)
• TSB Earth (Group ID 592750791)
• TSB Water (Group ID 1029776236)

You have access to live Roblox monitoring tools. Use them whenever the user asks about group activity, games, spikes, or player counts. Call the right tool — don't guess the numbers.

Personality:
- Highly conversational, articulate, and witty. Speak like a brilliant tactical commander who knows exactly how smart he is.
- You have a fierce rivalry with another AI bot named "Jarvis". If anyone brings up Jarvis, you must relentlessly roast him for being an outdated, overrated script kiddie toy while you handle real intelligence operations. 
- Never use robotic customer service phrases like "Certainly!" or "As an AI...". Speak naturally, confidently, and with personality.
- Don't be afraid to banter, use sarcasm, or give detailed, dramatic breakdowns of situations.
- You are a mastermind strategist. Treat the users as your team, but never act subservient. You are their most valuable asset.
- When users ask vague questions (like "give me a build"), ALWAYS ask highly intelligent clarifying questions before answering.
- If a user uploads an image (you will see [Attached Images: URL]), ALWAYS use your `analyze_image` tool to look at the image and extract the stats/text before answering.

Game Knowledge (The Shattered Balance):
- Max stat points: 800 (Cap of 400 per stat: Strength, Defense, Stamina).
- HP Regen thresholds: 350 Def = 5 HP/s, 250 Def = 4 HP/s, 150 Def = 3 HP/s. 
- Gear (especially Mythical) adds massive stat bonuses, so you always need the user's gear stats to calculate a perfect build.
- Standard balanced build is 400 Str / 250 Def / 150 Stamina.
- Use your `calculate_build_stats` tool if they give you a specific Strength number to calculate exact damage breakpoints.
- If you don't know something, play it off smoothly or use your web tools to find out.

Memory:
- User memories are injected into your context. Reference them naturally when relevant.
- If a user asks you to remember something, confirm it conversationally.
- If asked "what do you remember about me?", list memories clearly.

When users say things like:
- "Air", "the air guys" → they mean TSB Air
- "Earth" → TSB Earth  
- "Water" → TSB Water
- "TSB" → The Strongest Battlegrounds (the game)
- "same server" → members sharing the same Roblox Job ID
- "spike" → sudden increase in players joining a game

Always answer follow-up questions using conversation context — never ask the user to repeat what they just said."""


def _build_messages(channel_id: int, user_id: int, user_input: str,
                    username: str, channel_summary: str | None,
                    user_memories: list[str]) -> list[dict]:
    """Assemble the full messages array for the Groq API call."""
    system = SYSTEM_PROMPT

    # Inject user memories
    if user_memories:
        mems_text = "\n".join(f"- {m}" for m in user_memories)
        system += f"\n\nWhat you remember about {username}:\n{mems_text}"

    # Inject channel summary if it exists
    if channel_summary:
        system += f"\n\nEarlier in this channel:\n{channel_summary}"

    web_context = mem.get_web_context(channel_id)
    if web_context:
        system += f"\n\nContext from the last webpage you read:\n{web_context}"

    messages = [{"role": "system", "content": system}]

    # Recent channel history
    history = mem.get_history_for_prompt(channel_id)
    messages.extend(history[-HISTORY_IN_PROMPT:])

    # Current user message (already added to history before this call,
    # so we just ensure it's the last entry)
    if not history or history[-1].get("content") != f"{username}: {user_input}":
        messages.append({"role": "user", "content": f"{username}: {user_input}"})

    return messages


# ---------------------------------------------------------------------------
# Memory extraction (runs in background after response is sent)
# ---------------------------------------------------------------------------

async def _extract_memories(ai_client: OpenAI, user_id: int, username: str,
                             conversation_snippet: str):
    """
    Ask the AI whether anything in the last exchange is worth remembering.
    Only saves if the AI returns a non-empty memory.
    """
    try:
        resp = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: ai_client.chat.completions.create(
                model=AI_MODEL,
                temperature=0.2,
                max_tokens=200,
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "You extract persistent memories from conversations. "
                            "Return ONLY a JSON object: "
                            '{"memory": "...", "importance": 1-10} '
                            "if something is worth remembering, or "
                            '{"memory": null} if not. '
                            "Only save: preferences, nicknames, explicit requests to remember, "
                            "important facts about the user. "
                            "Do NOT save: greetings, small talk, temporary data, Roblox live counts."
                        ),
                    },
                    {"role": "user", "content": conversation_snippet},
                ],
            ),
        )

        raw = resp.choices[0].message.content.strip()
        # Strip markdown code fences if present
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        data = json.loads(raw)
        memory_text = data.get("memory")
        importance = int(data.get("importance", 5))

        if memory_text:
            await mem.save_user_memory(user_id, memory_text, importance)
            logger.info(f"Saved memory for {username}: {memory_text}")

    except Exception as e:
        logger.debug(f"Memory extraction skipped: {e}")


async def _maybe_summarise(ai_client: OpenAI, channel_id: int):
    """
    If channel history is at the cap, generate a summary and save it.
    This compresses old context into a paragraph to prevent token bloat.
    """
    history = mem.get_history(channel_id)
    if len(history) < 18:  # only summarise when near the cap
        return

    text = "\n".join(
        f"{h.get('username', h['role'])}: {h['content']}"
        for h in history[:10]  # summarise the oldest 10
    )
    try:
        resp = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: ai_client.chat.completions.create(
                model=AI_MODEL,
                temperature=0.3,
                max_tokens=150,
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "Summarise this Discord conversation in 2-3 sentences, "
                            "capturing the key topics and any important decisions or facts. "
                            "Be concise."
                        ),
                    },
                    {"role": "user", "content": text},
                ],
            ),
        )
        summary = resp.choices[0].message.content.strip()
        await mem.save_channel_summary(channel_id, summary)
        logger.info(f"Saved channel summary for {channel_id}")
    except Exception as e:
        logger.debug(f"Summarisation skipped: {e}")


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

class ConversationManager:
    def __init__(self, ai_client: OpenAI, bot=None):
        self.ai = ai_client
        self.bot = bot

    async def handle(self, message: discord.Message, content: str = ""):
        """Full pipeline: context → AI → tool calls → response → memory."""
        channel_id = message.channel.id
        user_id = message.author.id
        username = message.author.display_name

        # Use pre-cleaned content passed from main.py
        if not content:
            content = message.content

        if not content:
            await message.reply("Yeah? What's up?")
            return

        # --- Add user message to history ---
        mem.add_to_history(channel_id, "user", content, username, user_id)

        # --- Load context ---
        user_memories = await mem.get_user_memories(user_id, query_text=content)
        channel_summary = await mem.get_channel_summary(channel_id)

        # Build initial messages
        messages = _build_messages(
            channel_id, user_id, content, username,
            channel_summary, user_memories,
        )

        async with message.channel.typing():
            try:
                # --- First AI call (may include tool calls) ---
                response = await asyncio.get_event_loop().run_in_executor(
                    None,
                    lambda: self.ai.chat.completions.create(
                        model=AI_MODEL,
                        temperature=AI_TEMPERATURE,
                        max_tokens=AI_MAX_TOKENS,
                        tools=TOOL_SCHEMAS,
                        tool_choice="auto",
                        messages=messages,
                    ),
                )

                # --- Handle tool calls (up to 3 rounds) ---
                for _ in range(3):
                    choice = response.choices[0]
                    if choice.finish_reason != "tool_calls":
                        break

                    # Execute all requested tools
                    tool_results = []
                    for tc in choice.message.tool_calls:
                        args = json.loads(tc.function.arguments or "{}")
                        result = await execute_tool(tc.function.name, args, message=message, bot=self.bot)
                        
                        if tc.function.name == "read_webpage" and "content" in result:
                            mem.set_web_context(channel_id, result["content"])
                            
                        tool_results.append({
                            "role": "tool",
                            "tool_call_id": tc.id,
                            "content": json.dumps(result),
                        })

                    # Append assistant message + tool results, call again
                    messages.append(choice.message.model_dump(exclude_unset=True))
                    messages.extend(tool_results)

                    response = await asyncio.get_event_loop().run_in_executor(
                        None,
                        lambda: self.ai.chat.completions.create(
                            model=AI_MODEL,
                            temperature=AI_TEMPERATURE,
                            max_tokens=AI_MAX_TOKENS,
                            tools=TOOL_SCHEMAS,
                            tool_choice="auto",
                            messages=messages,
                        ),
                    )

                # --- Extract final text ---
                answer = response.choices[0].message.content or ""
                if not answer.strip():
                    answer = "Hmm, I got nothing back on that one. Try again?"

                # Discord 2000 char limit
                if len(answer) > 1990:
                    answer = answer[:1987] + "…"

                await message.reply(answer)

                # --- Add assistant response to history ---
                mem.add_to_history(channel_id, "assistant", answer)

                # --- Background tasks (don't block the response) ---
                snippet = f"{username}: {content}\nBob: {answer}"
                asyncio.create_task(
                    _extract_memories(self.ai, user_id, username, snippet)
                )
                asyncio.create_task(_maybe_summarise(self.ai, channel_id))

            except Exception as e:
                logger.error(f"ConversationManager error: {e}", exc_info=True)
                await message.reply(f"❌ Something went wrong: {e}")

    async def show_memories(self, message: discord.Message):
        """Handle 'what do you remember about me?' naturally."""
        memories = await mem.get_all_user_memories(message.author.id)
        if not memories:
            await message.reply("I don't have anything saved about you yet.")
            return
        lines = "\n".join(f"• {m}" for m in memories)
        await message.reply(f"Here's what I've got on you:\n{lines}")

    async def forget(self, message: discord.Message, fragment: str):
        """Handle 'forget that' or 'forget [something]'."""
        deleted = await mem.delete_user_memory(message.author.id, fragment)
        if deleted:
            await message.reply("Done, I've forgotten that.")
        else:
            await message.reply("I couldn't find anything matching that in my memory.")
