"""
Tool registry — assembles schemas and dispatches calls to the right module.

Intent-based tool selection keeps Groq token usage low.
Instead of sending all 38 tool schemas every request, we detect what the user
is likely asking about and only include the relevant categories.

Typical token savings: ~3,000 tokens → ~600-1,200 tokens per request.
"""

from .roblox_tools import ROBLOX_SCHEMAS, ROBLOX_TOOL_NAMES, execute_roblox_tool
from .discord_tools import DISCORD_SCHEMAS, DISCORD_TOOL_NAMES, execute_discord_tool
from .web_tools import WEB_SCHEMAS, WEB_TOOL_NAMES, execute_web_tool
from .vision_tools import VISION_SCHEMAS, VISION_TOOL_NAMES, execute_vision_tool
from .general_tools import (
    GENERAL_SCHEMAS,
    GENERAL_TOOL_NAMES,
    LOCAL_TASK_SCHEMA,
    execute_general_tool,
)
from .filesystem_tools import FILE_SCHEMAS, FILE_TOOL_NAMES, execute_filesystem_tool
from .fire_nation_tools import (
    MERIT_SCHEMAS,
    ORDER_SCHEMAS,
    ACCESS_SCHEMAS,
    FIRE_NATION_TOOL_NAMES,
    get_fire_nation_schemas,
    execute_fire_nation_tool,
)
import re

import config

# ---------------------------------------------------------------------------
# Intent detection — keyword sets for each tool category
# ---------------------------------------------------------------------------

_ROBLOX_KEYWORDS = frozenset([
    "tsb", "air", "earth", "water", "fire", "roblox", "group",
    "playing", "online", "game", "spike", "alert", "presence",
    "monitor", "universe", "concentration", "players", "in-game",
    "build", "strength", "defense", "stamina", "fireball", "damage",
    "shattered", "balance", "str", "def", "sta", "server count",
    "dashboard", "overview", "status", "summary", "live", "intelligence",
    "hr", "hrs", "high rank", "high ranks", "officer", "officers"
])

# Discord action keywords — these clearly indicate an admin operation
_DISCORD_ACTION_KEYWORDS = frozenset([
    "kick", "ban", "unban", "mute", "timeout", "unmute",
    "create channel", "delete channel", "rename channel", "make channel",
    "create role", "delete role", "edit role", "make role",
    "create category", "make category",
    "set permissions", "set perms", "lock", "unlock",
    "assign role", "give role", "remove role", "take role",
    "purge", "bulk delete", "clear messages", "pin message",
    "change nickname", "change nick",
    "send message to", "announce",
])

# Secondary Discord signals — these alone aren't enough, but help confirm intent
_DISCORD_SECONDARY_KEYWORDS = frozenset([
    "channel", "role", "category", "permissions", "perm",
    "server info", "server settings", "member info", "role info",
    "private", "restrict", "allow", "deny",
    "nickname", "nick", "staff", "mod", "admin", "vc", "voice",
    "voice channel", "text channel", "forum channel",
])

_WEB_KEYWORDS = frozenset([
    "search", "look up", "lookup", "find", "google", "latest", "recent",
    "current", "news", "update", "documentation", "docs", "wiki", "reddit",
    "website", "web", "trello", "http", "https", "weather", "price", "release",
])

_VOICE_KEYWORDS = frozenset([
    "join vc", "join voice", "leave vc", "leave voice",
    "hop in", "connect voice", "disconnect voice",
])

# Separate voice schemas from discord schemas for tighter selection
_VOICE_TOOL_NAMES = frozenset(["join_voice", "leave_voice"])
_DISCORD_ADMIN_SCHEMAS = [s for s in DISCORD_SCHEMAS if s["function"]["name"] not in _VOICE_TOOL_NAMES]
_VOICE_SCHEMAS = [s for s in DISCORD_SCHEMAS if s["function"]["name"] in _VOICE_TOOL_NAMES]

# Read-only info tools — safe to include more liberally
_INFO_TOOL_NAMES = frozenset(["get_server_info", "get_channel_info", "get_member_info", "get_role_info", "get_recent_messages"])
_INFO_SCHEMAS = [s for s in _DISCORD_ADMIN_SCHEMAS if s["function"]["name"] in _INFO_TOOL_NAMES]
_MODERATION_SCHEMAS = [s for s in _DISCORD_ADMIN_SCHEMAS if s["function"]["name"] not in _INFO_TOOL_NAMES]


def _wants_discord_admin(content: str) -> bool:
    """
    Return True only when the message is clearly asking for a Discord admin action.
    Uses a two-signal approach to avoid false positives on Roblox group queries.
    """
    lower = content.lower()
    # Multi-word action phrases are a strong signal on their own
    for phrase in _DISCORD_ACTION_KEYWORDS:
        if phrase in lower:
            return True
    # Single action verbs + a secondary Discord signal
    action_words = frozenset(["create", "delete", "remove", "rename", "make", "set", "give",
                               "assign", "add", "lock", "unlock", "restrict", "configure"])
    words = set(lower.split())
    has_action = bool(words & action_words)
    has_secondary = bool(words & _DISCORD_SECONDARY_KEYWORDS)
    return has_action and has_secondary


def get_tools_for_context(
    content: str,
    in_guild: bool = True,
    has_image: bool = False,
    member=None,
    recent_text: str = "",
) -> list[dict]:
    """
    Return only the tool schemas relevant to this specific message.

    This is the primary token-saving mechanism — instead of sending all 38
    tool definitions (≈3,000 tokens) every single request, we send only
    what the model actually needs (≈600–1,200 tokens for most requests).
    """
    lower = content.lower()
    words = set(lower.split())

    # Detect which categories are relevant
    wants_roblox = bool(words & _ROBLOX_KEYWORDS) or "how many" in lower or "who is" in lower
    wants_discord = in_guild and _wants_discord_admin(content)
    wants_voice = in_guild and any(phrase in lower for phrase in _VOICE_KEYWORDS)
    wants_web = bool(words & _WEB_KEYWORDS) or "http" in lower or "trello.com" in lower
    wants_vision = has_image

    schemas: list[dict] = []

    # Deterministic tools are always available, so the model can execute
    # arithmetic and time questions instead of guessing. The optional local
    # runner is only exposed when explicitly enabled in configuration.
    schemas.extend(GENERAL_SCHEMAS)
    if config.LOCAL_TASKS_ENABLED:
        schemas.append(LOCAL_TASK_SCHEMA)
        schemas.extend(FILE_SCHEMAS)

    # Always include Roblox tools — most queries in this server are Roblox-related
    schemas.extend(ROBLOX_SCHEMAS)

    # Web search — include when looking things up or a URL is present
    if config.FULL_TOOLSET or wants_web:
        schemas.extend(WEB_SCHEMAS)
    else:
        # Always include read_webpage so the AI can read any links shared
        schemas.append(next(s for s in WEB_SCHEMAS if s["function"]["name"] == "read_webpage"))

    # Vision — only when an image is attached or referenced
    if wants_vision or "image" in lower or "screenshot" in lower or "pic" in lower:
        schemas.extend(VISION_SCHEMAS)

    # In guilds, expose the complete Discord toolset by default. This is more
    # reliable than asking a keyword classifier to understand every phrasing
    # ("make a new VC", "remove the raid voice", etc.). The model still
    # chooses whether to call a tool, and executors enforce permissions.
    if in_guild and config.FULL_DISCORD_TOOLS:
        schemas.extend(DISCORD_SCHEMAS)
    elif wants_discord:
        schemas.extend(_MODERATION_SCHEMAS)

    # Discord info tools — include in guilds if user seems to be asking about server state
    info_words = frozenset(["info", "list", "show", "get", "what", "who", "how many", "check"])
    if in_guild and (bool(words & info_words) or wants_discord):
        schemas.extend(_INFO_SCHEMAS)

    # Merits and standing orders — matched against a short window of recent
    # user turns, so a reply to a clarifying question ("what reason should I
    # log?" → "log GG") doesn't drop the tool mid-flow.
    if in_guild:
        schemas.extend(get_fire_nation_schemas(
            f"{recent_text} {content}", member, offer_all=config.FULL_TOOLSET
        ))

    # Voice — join/leave vc
    if wants_voice and not (in_guild and config.FULL_DISCORD_TOOLS):
        schemas.extend(_VOICE_SCHEMAS)

    # Deduplicate while preserving order
    seen: set[str] = set()
    unique: list[dict] = []
    for s in schemas:
        name = s["function"]["name"]
        if name not in seen:
            seen.add(name)
            unique.append(s)

    # The dashboard posts a large embed, and the model kept reaching for it on
    # messages that had nothing to do with it. So it is only on the table when
    # the message asks for it — decided here, not left to the model.
    if not wants_dashboard(content, recent_text):
        unique = [s for s in unique if s["function"]["name"] != "send_dashboard"]

    return unique


_DASHBOARD_INTENT = re.compile(
    r"\b(dash\w*|overview|sit\W?rep|live (stats?|intel\w*|feed|data|numbers)|intel(ligence)?"
    r"|(status|summary|rundown|stats?) (of|on|for) (the |all |every )?(groups?|tribes?|nations?|servers?|everyone)"
    r"|all (the |three |3 )?groups)\b",
    re.IGNORECASE,
)
_AGAIN = re.compile(r"\b(again|refresh|resend|update it|once more|another one|one more time)\b", re.IGNORECASE)


def wants_dashboard(content: str, recent_text: str = "") -> bool:
    """True when this message asks for the dashboard, or asks to repeat one that was just requested."""
    if _DASHBOARD_INTENT.search(content):
        return True
    return len(content.split()) <= 6 and bool(_AGAIN.search(content)) and bool(_DASHBOARD_INTENT.search(recent_text))


def get_all_schemas(in_guild: bool = True) -> list[dict]:
    """Return ALL tool schemas. Prefer get_tools_for_context() to save tokens."""
    schemas = list(GENERAL_SCHEMAS) + list(ROBLOX_SCHEMAS) + list(WEB_SCHEMAS) + list(VISION_SCHEMAS)
    if config.LOCAL_TASKS_ENABLED:
        schemas.append(LOCAL_TASK_SCHEMA)
        schemas.extend(FILE_SCHEMAS)
    if in_guild:
        schemas += list(DISCORD_SCHEMAS) + list(MERIT_SCHEMAS) + list(ORDER_SCHEMAS) + list(ACCESS_SCHEMAS)
    return schemas


async def execute_tool(name: str, args: dict, ctx: dict) -> dict:
    """
    Dispatch a tool call by name.

    ctx dict keys:
      message        discord.Message — current message
      bot            commands.Bot
      voice_manager  voice.manager.VoiceManager (optional)
      channel_id     int
    """
    if name in ROBLOX_TOOL_NAMES:
        return await execute_roblox_tool(name, args, ctx=ctx)
    elif name in GENERAL_TOOL_NAMES:
        return await execute_general_tool(name, args, ctx)
    elif name in FILE_TOOL_NAMES:
        return await execute_filesystem_tool(name, args, ctx)
    elif name in DISCORD_TOOL_NAMES:
        return await execute_discord_tool(name, args, ctx)
    elif name in FIRE_NATION_TOOL_NAMES:
        return await execute_fire_nation_tool(name, args, ctx)
    elif name in WEB_TOOL_NAMES:
        result = await execute_web_tool(name, args)
        # Cache webpage content in channel memory for follow-up questions
        if name == "read_webpage" and "content" in result and ctx.get("channel_id"):
            from bob.conversation import memory as mem
            mem.set_web_context(ctx["channel_id"], result["content"])
        return result
    elif name in VISION_TOOL_NAMES:
        return await execute_vision_tool(name, args)
    else:
        return {"error": f"Unknown tool: '{name}'"}


__all__ = ["get_tools_for_context", "get_all_schemas", "execute_tool"]
