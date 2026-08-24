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
    "nickname", "nick", "staff", "mod", "admin",
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

    # Always include Roblox tools — most queries in this server are Roblox-related
    schemas.extend(ROBLOX_SCHEMAS)

    # Web search — include when looking things up or a URL is present
    if wants_web:
        schemas.extend(WEB_SCHEMAS)
    else:
        # Always include read_webpage so the AI can read any links shared
        schemas.append(next(s for s in WEB_SCHEMAS if s["function"]["name"] == "read_webpage"))

    # Vision — only when an image is attached or referenced
    if wants_vision or "image" in lower or "screenshot" in lower or "pic" in lower:
        schemas.extend(VISION_SCHEMAS)

    # Discord admin tools — only when the request is clearly administrative
    if wants_discord:
        schemas.extend(_MODERATION_SCHEMAS)

    # Discord info tools — include in guilds if user seems to be asking about server state
    info_words = frozenset(["info", "list", "show", "get", "what", "who", "how many", "check"])
    if in_guild and (bool(words & info_words) or wants_discord):
        schemas.extend(_INFO_SCHEMAS)

    # Voice — join/leave vc
    if wants_voice:
        schemas.extend(_VOICE_SCHEMAS)

    # Deduplicate while preserving order
    seen: set[str] = set()
    unique: list[dict] = []
    for s in schemas:
        name = s["function"]["name"]
        if name not in seen:
            seen.add(name)
            unique.append(s)

    return unique


def get_all_schemas(in_guild: bool = True) -> list[dict]:
    """Return ALL tool schemas. Prefer get_tools_for_context() to save tokens."""
    schemas = list(ROBLOX_SCHEMAS) + list(WEB_SCHEMAS) + list(VISION_SCHEMAS)
    if in_guild:
        schemas += list(DISCORD_SCHEMAS)
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
    elif name in DISCORD_TOOL_NAMES:
        return await execute_discord_tool(name, args, ctx)
    elif name in WEB_TOOL_NAMES:
        result = await execute_web_tool(name, args)
        # Cache webpage content in channel memory for follow-up questions
        if name == "read_webpage" and "content" in result and ctx.get("channel_id"):
            from conversation import memory as mem
            mem.set_web_context(ctx["channel_id"], result["content"])
        return result
    elif name in VISION_TOOL_NAMES:
        return await execute_vision_tool(name, args)
    else:
        return {"error": f"Unknown tool: '{name}'"}


__all__ = ["get_tools_for_context", "get_all_schemas", "execute_tool"]
