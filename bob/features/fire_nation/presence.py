"""
Presence — optional avatar/banner artwork, and sleep mode.

"bob go to sleep" / "bob good night" takes Bob fully offline: presence goes
invisible, the avatar swaps to the offline image, and every chat message is
ignored until "bob wake up".
"""

import asyncio
import logging
import os
import re

import discord
import httpx

import config

logger = logging.getLogger("discord")

SLEEP_PATTERN = re.compile(r"^bob[,]?\s+(?:go to sleep|good\s*night)[.!]?$", re.IGNORECASE)
WAKE_UP_PATTERN = re.compile(r"^bob[,]?\s+wake up[.!]?$", re.IGNORECASE)

AVATAR_ROTATION_SECONDS = 4 * 60 * 60

# ─── Optional artwork ─────────────────────────────────────────────────────────
# Drop files with these names into assets/ to enable them. They are read once
# at import; a missing file just means that visual is skipped.

_ASSETS_DIR = os.path.join(os.path.dirname(os.path.abspath(config.__file__)), "assets")


def _read_asset(*names: str) -> bytes | None:
    for name in names:
        try:
            with open(os.path.join(_ASSETS_DIR, name), "rb") as f:
                return f.read()
        except OSError:
            continue
    return None


ONLINE_AVATAR = _read_asset("avatar-online.gif", "avatar-online.png")
OFFLINE_AVATAR = _read_asset("avatar-offline.png", "avatar-offline.gif")
BANNER = _read_asset("banner.gif", "banner.png")

# ─── State ────────────────────────────────────────────────────────────────────

_asleep = False
_started = False
_tasks: list[asyncio.Task] = []


def is_asleep() -> bool:
    return _asleep


async def _edit_profile(bot: discord.Client, **fields) -> None:
    try:
        await bot.user.edit(**fields)
    except (discord.HTTPException, TypeError, ValueError) as e:
        # Rate-limited, or an animated image the bot account isn't eligible for.
        logger.warning(f"Could not update bot profile ({', '.join(fields)}): {e}")


async def _avatar_loop(bot: discord.Client) -> None:
    index = 0
    while True:
        await asyncio.sleep(AVATAR_ROTATION_SECONDS)
        try:
            async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
                res = await client.get(config.AVATAR_URLS[index % len(config.AVATAR_URLS)])
                res.raise_for_status()
            await _edit_profile(bot, avatar=res.content)
        except httpx.HTTPError:
            pass  # bad URL — skip to the next one
        index += 1


async def start(bot: discord.Client) -> None:
    """Starts avatar rotation and applies the online avatar/banner. Safe to call on every on_ready."""
    global _started
    if _started:
        return
    _started = True
    if config.AVATAR_URLS:
        _tasks.append(asyncio.create_task(_avatar_loop(bot)))
    if ONLINE_AVATAR:
        await _edit_profile(bot, avatar=ONLINE_AVATAR)
    if BANNER:
        await _edit_profile(bot, banner=BANNER)


def stop() -> None:
    for task in _tasks:
        task.cancel()
    _tasks.clear()


async def go_to_sleep(bot: discord.Client) -> None:
    global _asleep
    _asleep = True
    await bot.change_presence(status=discord.Status.invisible)
    if OFFLINE_AVATAR:
        await _edit_profile(bot, avatar=OFFLINE_AVATAR)


async def wake_up(bot: discord.Client) -> None:
    global _asleep
    _asleep = False
    if ONLINE_AVATAR:
        await _edit_profile(bot, avatar=ONLINE_AVATAR)
    await bot.change_presence(status=discord.Status.online)


async def on_shutdown(bot: discord.Client) -> None:
    """Switch to the offline avatar before the bot disconnects."""
    stop()
    if OFFLINE_AVATAR and bot.user and not bot.is_closed():
        await _edit_profile(bot, avatar=OFFLINE_AVATAR)
