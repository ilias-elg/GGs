"""
Voice control shared by /voice and the chat phrases ("bob, join vc"), plus
the things Bob does on his own while in voice: greeting the people he works
for when they join, and reading announcement-channel posts aloud.

Every control function returns the exact reply to show, so the slash command
and the chat phrase behave identically.
"""

import asyncio
import logging
import re
import time

import discord

import config

from . import greetings, tone as tone_feature
from .ranks import has_access
from .tts import clean_for_speech

logger = logging.getLogger("discord")

JOIN_LINE = "Voice systems online."


async def join_member_channel(vm, member: discord.Member) -> str:
    channel = member.voice.channel if member.voice else None
    if not channel:
        return "You'll need to be in a voice channel first — I join whichever one you're in."
    perms = channel.permissions_for(member.guild.me)
    if not (perms.view_channel and perms.connect and perms.speak):
        return f"I don't have permission to join and speak in {channel.name}."

    current = vm.current_channel(member.guild)
    if current and current.id == channel.id:
        return f"I'm already in **{channel.name}**."
    joined = await vm.join(member.guild, channel)
    if "error" in joined:
        return f"I couldn't join {channel.name} — {joined['error']}"
    # Don't hold the reply up waiting for the lines to finish playing.
    asyncio.create_task(vm.speak(member.guild, JOIN_LINE, cache=True))
    asyncio.create_task(greet_present_members(vm, channel, member.id))
    return f"Joined **{channel.name}**."


async def leave_member_guild(vm, member: discord.Member) -> str:
    left = await vm.leave(member.guild, f"asked by {member.display_name}")
    return "Leaving voice." if left.get("success") else "I'm not in a voice channel on this server."


async def say_in_voice(vm, member: discord.Member, text: str, tone: str | None = None) -> str:
    """
    Speaks `text` and reports honestly whether it actually played. The tone
    comes from `tone` when one is chosen, otherwise from how the text is
    written — read before it is cleaned, since capitals, emoji and "/s" are
    exactly what cleaning removes.
    """
    if not vm.current_channel(member.guild):
        return 'I\'m not in a voice channel — say "bob, join vc" or use `/voice join` first.'
    chosen, spoken = tone_feature.tone_for(text, tone)
    line = clean_for_speech(spoken, member.guild)
    if not line:
        return "There's nothing there I can actually say aloud."
    ok, reason = await vm.speak(member.guild, line, delivery=tone_feature.delivery_for(chosen))
    if not ok:
        return f"I couldn't say that aloud — {reason}"
    return f"Said aloud — tone: {chosen.label.lower() if chosen else 'neutral'}."


def voice_status_report(vm, guild: discord.Guild) -> str:
    """Where Bob is and what happened recently."""
    channel = vm.current_channel(guild)
    if channel:
        leaving = (
            ", leaving in under 15 min unless someone joins" if vm.alone_timer_running(guild.id) else ""
        )
        head = f"In <#{channel.id}>, connected{leaving}."
    else:
        head = "Not in a voice channel."
    recent = vm.recent_events(guild.id)
    log = "\n".join(f"• <t:{at}:T> {text}" for at, text in recent) or "• nothing yet since my last restart"
    return f"**Voice status:** {head}\n**Recent voice activity:**\n{log}"


# ─── Chat phrases (handled before the AI — no tokens spent) ──────────────────

_NAME = r"(?:bob[,]?\s+)?"
_JOIN = re.compile(
    rf"^{_NAME}(?:join|hop in(?:to)?|get in(?:to)?|come to)\s+(?:the\s+|my\s+)?(?:vc|voice(?: chat| channel)?|call)\b[.!]?$",
    re.IGNORECASE,
)
_LEAVE = re.compile(
    rf"^{_NAME}(?:leave|get out of|disconnect from|exit)\s+(?:the\s+)?(?:vc|voice(?: chat| channel)?|call)\b[.!]?$",
    re.IGNORECASE,
)
_SAY_IT = re.compile(
    rf"^{_NAME}(?:(?:say|read)\s+(?:that|it|this)\s+(?:out\s+loud|aloud)|speak\s+(?:that|it|up))\b[.!]?$",
    re.IGNORECASE,
)
_STATUS = re.compile(
    rf"^{_NAME}(?:(?:voice|vc)\s+(?:status|log|report)|what(?:'s| is| happened) (?:with |in )?(?:the )?(?:voice|vc)"
    r"|are you in (?:the )?(?:vc|voice))\b[.!?]?$",
    re.IGNORECASE,
)


def match_voice_phrase(text: str) -> str | None:
    t = text.strip()
    if _JOIN.match(t):
        return "join"
    if _LEAVE.match(t):
        return "leave"
    if _SAY_IT.match(t):
        return "say_last"
    if _STATUS.match(t):
        return "status"
    return None


# ─── Greetings ────────────────────────────────────────────────────────────────
# When someone Bob works for — Owner, Fire Lord, or the standing-access list —
# joins the voice channel he's in, he greets them with their custom line
# (/voice greeting) or the default. Nobody else is ever greeted. Each line is
# cached, so repeats cost nothing.

# Only there to absorb connection flapping (a client dropping and rejoining
# within seconds) — a real leave-and-rejoin should always get a greeting.
GREETING_COOLDOWN_SECONDS = 30
# When someone joins, their client needs a moment to connect its audio. Audio
# sent in that window never reaches them, so the greeting waits briefly.
GREETING_DELAY_SECONDS = 2
_last_greeted: dict[tuple[int, int], float] = {}


async def handle_voice_state_update(
    vm, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState
) -> None:
    guild = member.guild
    if member.id == guild.me.id:
        # Bob himself was moved or disconnected.
        if after.channel is None:
            vm.event(guild.id, "disconnected from voice")
            vm.forget(guild.id)
        else:
            vm.update_alone_timer(guild)
        return

    bob_channel = vm.current_channel(guild)
    if not bob_channel:
        return
    # People came and went — re-check whether the channel is empty.
    vm.update_alone_timer(guild)

    if member.bot:
        return
    just_joined = after.channel == bob_channel and before.channel != bob_channel
    if not just_joined:
        return

    who = member.display_name
    if not has_access(member):
        vm.event(guild.id, f"{who} joined — not greeted (not on the access list)")
        return

    key = (guild.id, member.id)
    since_last = time.time() - _last_greeted.get(key, 0)
    if since_last < GREETING_COOLDOWN_SECONDS:
        vm.event(guild.id, f"{who} rejoined — not greeted (greeted {round(since_last)}s ago)")
        return
    _last_greeted[key] = time.time()

    await asyncio.sleep(GREETING_DELAY_SECONDS)
    if not member.voice or member.voice.channel != vm.current_channel(guild):
        vm.event(guild.id, f"{who} left again before the greeting — skipped")
        _last_greeted.pop(key, None)  # they never heard it, so don't hold it against the next join
        return

    ok, reason = await vm.speak(
        guild, greetings.greeting_for(member.id, who), cache=True, label=f"greeting for {who}"
    )
    if not ok:
        logger.warning(f"Voice greeting failed: {reason}")
        _last_greeted.pop(key, None)  # failed greetings shouldn't start the cooldown


# ─── Group greeting when Bob joins ───────────────────────────────────────────
# People already in the channel when Bob arrives never trigger a join event,
# so they get one combined line instead of a queue of greetings. Whoever
# summoned him is left out — the join line already answers them.

MAX_NAMES_SPOKEN = 5


def spoken_name(member: discord.Member) -> str:
    """A display name Bob can pronounce: no emoji or symbols, falls back to the username."""
    def clean(s: str) -> str:
        return " ".join(re.sub(r"[^\w' .-]+", " ", s).replace("_", " ").split())
    return clean(member.display_name) or clean(member.name) or "friend"


def group_greeting_line(names: list[str]) -> str:
    if not names:
        return ""
    if len(names) == 1:
        return f"Good to see you, {names[0]}."
    shown = names[:MAX_NAMES_SPOKEN]
    others = len(names) - len(shown)
    if others > 0:
        listing = f"{', '.join(shown)} and {others} other{'' if others == 1 else 's'}"
    else:
        listing = f"{', '.join(shown[:-1])} and {shown[-1]}"
    return f"Good to see you all — {listing}."


async def greet_present_members(vm, channel, summoner_id: int) -> None:
    """Greets everyone with access who was already in `channel` when Bob joined it."""
    guild = channel.guild
    present = [m for m in channel.members if not m.bot and m.id != summoner_id]
    greeted = [m for m in present if has_access(m)]
    if not greeted:
        if present:
            vm.event(guild.id, "nobody else here on the access list — no group greeting")
        return
    # Counts as their greeting, so someone who blips out and back isn't greeted twice.
    now = time.time()
    for m in greeted:
        _last_greeted[(guild.id, m.id)] = now

    ok, reason = await vm.speak(
        guild,
        group_greeting_line([spoken_name(m) for m in greeted]),
        label=f"group greeting for {len(greeted)} ({', '.join(m.display_name for m in greeted)})",
    )
    if not ok:
        logger.warning(f"Group voice greeting failed: {reason}")
        for m in greeted:
            _last_greeted.pop((guild.id, m.id), None)


# ─── Relayed announcements ───────────────────────────────────────────────────
# Posts in announcement channels are read out in the voice channel Bob is in
# on that server. "Announcement channels" = Discord's announcement-type
# channels, plus any IDs in ANNOUNCEMENT_CHANNEL_IDS for servers that use an
# ordinary text channel for announcements.


def is_announcement_channel(message: discord.Message) -> bool:
    return (
        message.channel.type == discord.ChannelType.news
        or message.channel.id in config.ANNOUNCEMENT_CHANNEL_IDS
    )


def announcement_speech(message: discord.Message) -> str:
    body = clean_for_speech(message.content, message.guild)
    if not body:
        return ""  # image-only / link-only posts: nothing worth saying
    return f"Announcement from {message.author.display_name}. {body}"


async def handle_announcement_message(vm, message: discord.Message) -> None:
    guild = message.guild
    if not guild or message.author.id == guild.me.id:
        return
    if not vm.current_channel(guild) or not is_announcement_channel(message):
        return
    line = announcement_speech(message)
    if not line:
        return
    ok, reason = await vm.speak(guild, line)
    if not ok:
        logger.warning(f"Could not read announcement aloud: {reason}")
