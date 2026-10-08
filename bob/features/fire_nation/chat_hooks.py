"""
Things handled directly from chat before the AI is involved — no tokens
spent: sleep/wake, and the voice phrases ("bob, join vc").
"""

import logging

import discord

from bob.conversation import memory as mem

from . import presence
from . import voice as voice_feature
from .ranks import has_access

logger = logging.getLogger("discord")


async def handle_sleep_wake(message: discord.Message, bot) -> str | None:
    """
    Returns "handled" when this message was a sleep/wake command, "asleep"
    when Bob is asleep and the message must be ignored, otherwise None.
    Only the Owner, Fire Lord and the access list can put Bob to sleep or
    wake him.
    """
    text = message.content.strip()
    authorized = has_access(message.author)

    if authorized and not presence.is_asleep() and presence.SLEEP_PATTERN.match(text):
        mem.end_conversation(message.channel.id)
        try:
            await presence.go_to_sleep(bot)
        except discord.HTTPException as e:
            logger.warning(f"Sleep: failed to fully go offline: {e}")
        await message.reply('Night. Say "bob, wake up" when you need me back online.')
        return "handled"

    if not presence.is_asleep():
        return None

    if authorized and presence.WAKE_UP_PATTERN.match(text):
        try:
            await presence.wake_up(bot)
        except discord.HTTPException as e:
            logger.warning(f"Wake: failed to restore presence: {e}")
        await message.reply("Back online. What do you need?")
        return "handled"
    # While asleep, every other message (including "bob") is ignored.
    return "asleep"


def _last_reply(channel_id: int) -> str | None:
    for entry in reversed(mem.get_history(channel_id)):
        if entry["role"] == "assistant" and entry["content"]:
            return entry["content"]
    return None


async def handle_voice_phrase(message: discord.Message, content: str, voice_manager) -> bool:
    """Handles "join vc" / "leave vc" / "say that out loud" / "voice status". True when it replied."""
    member = message.author
    if not message.guild or not isinstance(member, discord.Member) or not has_access(member):
        return False
    phrase = voice_feature.match_voice_phrase(content)
    if not phrase:
        return False

    async with message.channel.typing():
        if phrase == "join":
            reply = await voice_feature.join_member_channel(voice_manager, member)
        elif phrase == "leave":
            reply = await voice_feature.leave_member_guild(voice_manager, member)
        elif phrase == "status":
            reply = voice_feature.voice_status_report(voice_manager, message.guild, member)
        else:
            last = _last_reply(message.channel.id)
            reply = (
                await voice_feature.say_in_voice(voice_manager, member, last)
                if last
                else "I haven't said anything here yet that I could repeat."
            )
    await message.reply(reply)
    mem.add_to_history(message.channel.id, "assistant", reply)
    return True
