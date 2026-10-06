"""
Private error reports. When something fails, the people in the channel are not
told — the Owner gets a DM with what happened, where, and to whom.
"""

import logging
import time
from datetime import datetime, timezone

import discord

import config

logger = logging.getLogger("discord")

# The same failure repeating (an overloaded AI, say) would otherwise flood the
# Owner's DMs: one report per kind of error per window, with a count of the rest.
REPEAT_WINDOW_SECONDS = 5 * 60
_last_sent: dict[str, float] = {}
_suppressed: dict[str, int] = {}


def _trim(text: str, limit: int) -> str:
    text = str(text).strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


async def report_error(
    bot: discord.Client,
    error: BaseException,
    *,
    what: str,
    message: discord.Message | None = None,
    user: discord.abc.User | None = None,
    channel=None,
) -> None:
    """DMs every Owner about a failure. Never raises — reporting an error must not cause another."""
    try:
        kind = f"{what}:{type(error).__name__}"
        now = time.monotonic()
        if now - _last_sent.get(kind, -REPEAT_WINDOW_SECONDS) < REPEAT_WINDOW_SECONDS:
            _suppressed[kind] = _suppressed.get(kind, 0) + 1
            return
        repeats = _suppressed.pop(kind, 0)
        _last_sent[kind] = now

        user = user or (message.author if message else None)
        channel = channel or (message.channel if message else None)
        guild = getattr(channel, "guild", None)

        embed = discord.Embed(
            title=f"Error: {what}",
            description=f"```\n{_trim(f'{type(error).__name__}: {error}', 1500)}\n```",
            color=config.FIRE_RED,
            timestamp=datetime.now(timezone.utc),
        )
        if user:
            embed.add_field(name="Who", value=f"<@{user.id}> {discord.utils.escape_markdown(str(user))}", inline=True)
        if channel is not None:
            escape = discord.utils.escape_markdown
            where = f"#{escape(getattr(channel, 'name', None) or 'DM')}" + (f" in {escape(guild.name)}" if guild else "")
            embed.add_field(name="Where", value=where, inline=True)
        if message is not None:
            if message.content:
                embed.add_field(name="They said", value=_trim(message.content, 500), inline=False)
            embed.add_field(name="Message", value=f"[Open it]({message.jump_url})", inline=False)
        if repeats:
            embed.set_footer(text=f"{repeats} more like this in the last few minutes were not sent.")

        delivered = 0
        for owner_id in config.OWNER_USER_IDS:
            try:
                owner = bot.get_user(owner_id) or await bot.fetch_user(owner_id)
                await owner.send(embed=embed)
                delivered += 1
            except discord.HTTPException as e:
                logger.warning(f"Could not DM owner {owner_id} about an error: {e}")
        if not delivered:
            logger.warning("An error report reached no Owner — check DISCORD_OWNER_USER_IDS and that their DMs are open.")
    except Exception as e:
        logger.warning(f"Error report failed: {e}")
