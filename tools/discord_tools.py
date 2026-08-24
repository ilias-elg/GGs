"""
Discord administrative tools — permission-checked, safe, explicit.

Each tool:
  1. Validates the bot has the required permission.
  2. Validates the requesting user has the required permission.
  3. Checks role hierarchy where relevant.
  4. Executes the action.
  5. Returns a structured result dict.

These replace the old execute_discord_python "god mode" tool entirely.
All Discord objects are resolved by name OR ID for natural-language friendliness.
"""

import asyncio
import logging
from datetime import timedelta, timezone, datetime
from typing import Optional

import discord
from discord.ext import commands

logger = logging.getLogger("discord")

# ---------------------------------------------------------------------------
# Tool schemas
# ---------------------------------------------------------------------------

DISCORD_SCHEMAS: list[dict] = [
    # ── Server management ────────────────────────────────────────────────────
    {
        "type": "function",
        "function": {
            "name": "create_channel",
            "description": (
                "Create a new text, voice, or other channel in the server. "
                "Optionally place it inside an existing category. "
                "Use private=true to make it visible only to roles you specify later."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Channel name"},
                    "channel_type": {
                        "type": "string",
                        "enum": ["text", "voice", "forum", "stage"],
                        "description": "Channel type. Default: text",
                    },
                    "category": {
                        "type": "string",
                        "description": "Category name or ID to place the channel in. Optional.",
                    },
                    "topic": {"type": "string", "description": "Channel topic (text channels only)"},
                    "private": {
                        "type": "boolean",
                        "description": "If true, deny @everyone view access so only permitted roles can see it.",
                    },
                    "reason": {"type": "string", "description": "Reason shown in audit log"},
                },
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_channel",
            "description": "Permanently delete a channel from the server.",
            "parameters": {
                "type": "object",
                "properties": {
                    "channel": {"type": "string", "description": "Channel name or ID"},
                    "reason": {"type": "string", "description": "Reason for audit log"},
                },
                "required": ["channel"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "rename_channel",
            "description": "Rename an existing channel.",
            "parameters": {
                "type": "object",
                "properties": {
                    "channel": {"type": "string", "description": "Current channel name or ID"},
                    "new_name": {"type": "string", "description": "New name for the channel"},
                    "reason": {"type": "string", "description": "Reason for audit log"},
                },
                "required": ["channel", "new_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit_channel",
            "description": "Edit a channel's properties: topic, slowmode, NSFW flag, etc.",
            "parameters": {
                "type": "object",
                "properties": {
                    "channel": {"type": "string", "description": "Channel name or ID"},
                    "topic": {"type": "string", "description": "New topic/description"},
                    "slowmode_delay": {"type": "integer", "description": "Slowmode in seconds (0 to disable)"},
                    "nsfw": {"type": "boolean", "description": "Mark channel as age-restricted"},
                    "reason": {"type": "string", "description": "Reason for audit log"},
                },
                "required": ["channel"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_category",
            "description": "Create a new channel category.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Category name"},
                    "private": {
                        "type": "boolean",
                        "description": "If true, deny @everyone view access.",
                    },
                    "reason": {"type": "string", "description": "Reason for audit log"},
                },
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_channel_permissions",
            "description": (
                "Set permission overwrites for a role or member in a specific channel. "
                "Use to make channels private, read-only, restrict posting, etc. "
                "Permissions list example: ['send_messages', 'view_channel', 'read_message_history']"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "channel": {"type": "string", "description": "Channel name or ID"},
                    "target": {"type": "string", "description": "Role name/ID or member username/ID"},
                    "target_type": {
                        "type": "string",
                        "enum": ["role", "member"],
                        "description": "Whether target is a role or a member",
                    },
                    "allow": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Permissions to explicitly allow",
                    },
                    "deny": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Permissions to explicitly deny",
                    },
                    "reason": {"type": "string", "description": "Reason for audit log"},
                },
                "required": ["channel", "target", "target_type"],
            },
        },
    },
    # ── Role management ──────────────────────────────────────────────────────
    {
        "type": "function",
        "function": {
            "name": "create_role",
            "description": "Create a new role in the server.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Role name"},
                    "color": {
                        "type": "string",
                        "description": "Role color as hex string (e.g. #FF0000) or a color name like 'red', 'blue'.",
                    },
                    "hoist": {
                        "type": "boolean",
                        "description": "Show role members separately in the member list",
                    },
                    "mentionable": {
                        "type": "boolean",
                        "description": "Allow anyone to @mention this role",
                    },
                    "reason": {"type": "string", "description": "Reason for audit log"},
                },
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_role",
            "description": "Delete a role from the server.",
            "parameters": {
                "type": "object",
                "properties": {
                    "role": {"type": "string", "description": "Role name or ID"},
                    "reason": {"type": "string", "description": "Reason for audit log"},
                },
                "required": ["role"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit_role",
            "description": "Edit a role's name, color, hoist setting, or mentionability.",
            "parameters": {
                "type": "object",
                "properties": {
                    "role": {"type": "string", "description": "Role name or ID"},
                    "name": {"type": "string", "description": "New name"},
                    "color": {"type": "string", "description": "New color (hex or name)"},
                    "hoist": {"type": "boolean", "description": "Show separately in member list"},
                    "mentionable": {"type": "boolean", "description": "Allow @mentions"},
                    "reason": {"type": "string", "description": "Reason for audit log"},
                },
                "required": ["role"],
            },
        },
    },
    # ── Member management ────────────────────────────────────────────────────
    {
        "type": "function",
        "function": {
            "name": "kick_member",
            "description": "Kick a member from the server.",
            "parameters": {
                "type": "object",
                "properties": {
                    "member": {"type": "string", "description": "Member username, display name, or ID"},
                    "reason": {"type": "string", "description": "Reason for the kick"},
                },
                "required": ["member"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "ban_member",
            "description": "Ban a member from the server.",
            "parameters": {
                "type": "object",
                "properties": {
                    "member": {"type": "string", "description": "Member username, display name, or ID"},
                    "reason": {"type": "string", "description": "Reason for the ban"},
                    "delete_message_days": {
                        "type": "integer",
                        "description": "Delete their messages from the last N days (0-7, default 0)",
                    },
                },
                "required": ["member"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "unban_member",
            "description": "Unban a previously banned user.",
            "parameters": {
                "type": "object",
                "properties": {
                    "user": {"type": "string", "description": "Username or user ID to unban"},
                    "reason": {"type": "string", "description": "Reason for audit log"},
                },
                "required": ["user"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "timeout_member",
            "description": "Timeout (mute) a member for a specified duration. They can't send messages or join VCs.",
            "parameters": {
                "type": "object",
                "properties": {
                    "member": {"type": "string", "description": "Member username, display name, or ID"},
                    "duration_minutes": {
                        "type": "number",
                        "description": "Timeout duration in minutes (max 40320 = 28 days)",
                    },
                    "reason": {"type": "string", "description": "Reason for the timeout"},
                },
                "required": ["member", "duration_minutes"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "remove_timeout",
            "description": "Remove an active timeout from a member.",
            "parameters": {
                "type": "object",
                "properties": {
                    "member": {"type": "string", "description": "Member username, display name, or ID"},
                },
                "required": ["member"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "assign_role",
            "description": "Assign a role to a member.",
            "parameters": {
                "type": "object",
                "properties": {
                    "member": {"type": "string", "description": "Member username, display name, or ID"},
                    "role": {"type": "string", "description": "Role name or ID to assign"},
                    "reason": {"type": "string", "description": "Reason for audit log"},
                },
                "required": ["member", "role"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "remove_role",
            "description": "Remove a role from a member.",
            "parameters": {
                "type": "object",
                "properties": {
                    "member": {"type": "string", "description": "Member username, display name, or ID"},
                    "role": {"type": "string", "description": "Role name or ID to remove"},
                    "reason": {"type": "string", "description": "Reason for audit log"},
                },
                "required": ["member", "role"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "change_nickname",
            "description": "Change a member's server nickname. Pass empty string to remove nickname.",
            "parameters": {
                "type": "object",
                "properties": {
                    "member": {"type": "string", "description": "Member username, display name, or ID"},
                    "nickname": {"type": "string", "description": "New nickname (empty string removes it)"},
                },
                "required": ["member", "nickname"],
            },
        },
    },
    # ── Message management ───────────────────────────────────────────────────
    {
        "type": "function",
        "function": {
            "name": "send_message",
            "description": "Send a message to a specific channel.",
            "parameters": {
                "type": "object",
                "properties": {
                    "channel": {"type": "string", "description": "Channel name or ID to send to"},
                    "content": {"type": "string", "description": "Message content"},
                },
                "required": ["channel", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_message",
            "description": "Delete a specific message by its ID.",
            "parameters": {
                "type": "object",
                "properties": {
                    "channel": {"type": "string", "description": "Channel name or ID"},
                    "message_id": {"type": "string", "description": "The message ID to delete"},
                    "reason": {"type": "string", "description": "Reason for audit log"},
                },
                "required": ["channel", "message_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "bulk_delete_messages",
            "description": (
                "Bulk delete recent messages from a channel. "
                "Only works on messages less than 14 days old (Discord API limitation)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "channel": {"type": "string", "description": "Channel name or ID"},
                    "count": {
                        "type": "integer",
                        "description": "Number of messages to delete (1-100)",
                    },
                    "reason": {"type": "string", "description": "Reason for audit log"},
                },
                "required": ["channel", "count"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "pin_message",
            "description": "Pin a message in a channel.",
            "parameters": {
                "type": "object",
                "properties": {
                    "channel": {"type": "string", "description": "Channel name or ID"},
                    "message_id": {"type": "string", "description": "Message ID to pin"},
                },
                "required": ["channel", "message_id"],
            },
        },
    },
    # ── Information ──────────────────────────────────────────────────────────
    {
        "type": "function",
        "function": {
            "name": "get_server_info",
            "description": "Get information about the current Discord server: member count, channels, roles, etc.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_channel_info",
            "description": "Get information about a specific channel, or list all channels if none specified.",
            "parameters": {
                "type": "object",
                "properties": {
                    "channel": {"type": "string", "description": "Channel name or ID. Omit to list all."},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_member_info",
            "description": "Get information about a specific member: roles, join date, permissions, timeout status.",
            "parameters": {
                "type": "object",
                "properties": {
                    "member": {"type": "string", "description": "Member username, display name, or ID"},
                },
                "required": ["member"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_role_info",
            "description": "Get information about a specific role or list all roles.",
            "parameters": {
                "type": "object",
                "properties": {
                    "role": {"type": "string", "description": "Role name or ID. Omit to list all roles."},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_recent_messages",
            "description": "Retrieve recent messages from a channel for reading context or searching.",
            "parameters": {
                "type": "object",
                "properties": {
                    "channel": {
                        "type": "string",
                        "description": "Channel name or ID. Defaults to current channel.",
                    },
                    "count": {
                        "type": "integer",
                        "description": "Number of recent messages to retrieve (max 50, default 10)",
                    },
                },
            },
        },
    },
    # ── Voice ────────────────────────────────────────────────────────────────
    {
        "type": "function",
        "function": {
            "name": "join_voice",
            "description": (
                "Join a voice channel. If no channel is specified, joins the voice "
                "channel the requesting user is currently in."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "channel": {
                        "type": "string",
                        "description": "Voice channel name or ID. Optional — defaults to user's current VC.",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "leave_voice",
            "description": "Leave the current voice channel.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
]

DISCORD_TOOL_NAMES: frozenset[str] = frozenset(s["function"]["name"] for s in DISCORD_SCHEMAS)

# ---------------------------------------------------------------------------
# Resolution helpers
# ---------------------------------------------------------------------------


def _resolve_channel(
    guild: discord.Guild, name_or_id: str
) -> discord.abc.GuildChannel | None:
    """Find a channel by name (case-insensitive, partial match) or ID."""
    name_or_id = name_or_id.strip()
    # Try ID first
    try:
        ch = guild.get_channel(int(name_or_id))
        if ch:
            return ch
    except ValueError:
        pass
    # Exact name match (Discord normalizes channel names to lowercase with dashes)
    normalized = name_or_id.lower().replace(" ", "-").replace("#", "")
    for ch in guild.channels:
        if ch.name.lower() == normalized:
            return ch
    # Partial match
    for ch in guild.channels:
        if normalized in ch.name.lower():
            return ch
    return None


def _resolve_member(guild: discord.Guild, name_or_id: str) -> discord.Member | None:
    """Find a member by ID, username, or display name."""
    name_or_id = name_or_id.strip().lstrip("@")
    # Try ID
    try:
        m = guild.get_member(int(name_or_id))
        if m:
            return m
    except ValueError:
        pass
    lower = name_or_id.lower()
    # Exact display name or username
    for m in guild.members:
        if m.display_name.lower() == lower or m.name.lower() == lower:
            return m
    # Starts-with
    for m in guild.members:
        if m.display_name.lower().startswith(lower) or m.name.lower().startswith(lower):
            return m
    # Contains
    for m in guild.members:
        if lower in m.display_name.lower() or lower in m.name.lower():
            return m
    return None


def _resolve_role(guild: discord.Guild, name_or_id: str) -> discord.Role | None:
    """Find a role by ID or name."""
    name_or_id = name_or_id.strip().lstrip("@")
    try:
        r = guild.get_role(int(name_or_id))
        if r:
            return r
    except ValueError:
        pass
    lower = name_or_id.lower()
    for r in guild.roles:
        if r.name.lower() == lower:
            return r
    for r in guild.roles:
        if lower in r.name.lower():
            return r
    return None


def _parse_color(color_str: str | None) -> discord.Color | discord.Color:
    if not color_str:
        return discord.Color.default()
    color_str = color_str.strip()
    # Named colors
    named = {
        "red": discord.Color.red(), "blue": discord.Color.blue(),
        "green": discord.Color.green(), "yellow": discord.Color.yellow(),
        "orange": discord.Color.orange(), "purple": discord.Color.purple(),
        "gold": discord.Color.gold(), "teal": discord.Color.teal(),
        "magenta": discord.Color.magenta(), "dark_red": discord.Color.dark_red(),
        "dark_blue": discord.Color.dark_blue(), "dark_green": discord.Color.dark_green(),
        "white": discord.Color(0xffffff), "black": discord.Color(0x000000),
    }
    if color_str.lower() in named:
        return named[color_str.lower()]
    # Hex
    try:
        hex_str = color_str.lstrip("#")
        return discord.Color(int(hex_str, 16))
    except ValueError:
        return discord.Color.default()


def _build_perms_overwrite(allow: list[str] | None, deny: list[str] | None) -> discord.PermissionOverwrite:
    ow = discord.PermissionOverwrite()
    for p in (allow or []):
        try:
            setattr(ow, p, True)
        except AttributeError:
            pass
    for p in (deny or []):
        try:
            setattr(ow, p, False)
        except AttributeError:
            pass
    return ow


# ---------------------------------------------------------------------------
# Permission checks
# ---------------------------------------------------------------------------


def _check_bot_perm(bot_member: discord.Member, *perms: str) -> str | None:
    bot_perms = bot_member.guild_permissions
    if bot_perms.administrator:
        return None
    missing = [p for p in perms if not getattr(bot_perms, p, False)]
    if missing:
        return f"I'm missing the required permission(s): {', '.join(missing)}"
    return None


def _check_user_perm(member: discord.Member, *perms: str) -> str | None:
    if member.guild_permissions.administrator:
        return None
    missing = [p for p in perms if not getattr(member.guild_permissions, p, False)]
    if missing:
        return f"You don't have the required permission(s): {', '.join(missing)}"
    return None


def _check_hierarchy(
    bot_member: discord.Member, target_member: discord.Member
) -> str | None:
    if target_member.top_role >= bot_member.top_role:
        return (
            f"Can't act on {target_member.display_name} — "
            "their highest role is equal to or above mine in the hierarchy."
        )
    return None


# ---------------------------------------------------------------------------
# Main executor
# ---------------------------------------------------------------------------


async def execute_discord_tool(name: str, args: dict, ctx: dict) -> dict:
    """
    ctx must contain:
      message: discord.Message
      bot: commands.Bot
      voice_manager: voice.manager.VoiceManager (optional)
    """
    message: discord.Message = ctx["message"]
    bot: commands.Bot = ctx["bot"]
    guild: discord.Guild | None = message.guild

    if not guild:
        return {"error": "This command only works inside a server (guild)."}

    try:
        bot_member = guild.me or guild.get_member(bot.user.id)
        author = message.author if isinstance(message.author, discord.Member) else guild.get_member(message.author.id)

        # ── Server management ────────────────────────────────────────────────
        if name == "create_channel":
            return await _create_channel(guild, bot_member, author, args)
        elif name == "delete_channel":
            return await _delete_channel(guild, bot_member, author, args)
        elif name == "rename_channel":
            return await _rename_channel(guild, bot_member, author, args)
        elif name == "edit_channel":
            return await _edit_channel(guild, bot_member, author, args)
        elif name == "create_category":
            return await _create_category(guild, bot_member, author, args)
        elif name == "set_channel_permissions":
            return await _set_channel_permissions(guild, bot_member, author, args)

        # ── Role management ──────────────────────────────────────────────────
        elif name == "create_role":
            return await _create_role(guild, bot_member, author, args)
        elif name == "delete_role":
            return await _delete_role(guild, bot_member, author, args)
        elif name == "edit_role":
            return await _edit_role(guild, bot_member, author, args)

        # ── Member management ────────────────────────────────────────────────
        elif name == "kick_member":
            return await _kick_member(guild, bot_member, author, args)
        elif name == "ban_member":
            return await _ban_member(guild, bot_member, author, args)
        elif name == "unban_member":
            return await _unban_member(guild, bot_member, author, args)
        elif name == "timeout_member":
            return await _timeout_member(guild, bot_member, author, args)
        elif name == "remove_timeout":
            return await _remove_timeout(guild, bot_member, author, args)
        elif name == "assign_role":
            return await _assign_role(guild, bot_member, author, args)
        elif name == "remove_role":
            return await _remove_role(guild, bot_member, author, args)
        elif name == "change_nickname":
            return await _change_nickname(guild, bot_member, author, args)

        # ── Message management ───────────────────────────────────────────────
        elif name == "send_message":
            return await _send_message(guild, bot_member, author, args)
        elif name == "delete_message":
            return await _delete_message(guild, bot_member, author, args)
        elif name == "bulk_delete_messages":
            return await _bulk_delete_messages(guild, bot_member, author, args)
        elif name == "pin_message":
            return await _pin_message(guild, bot_member, author, args)

        # ── Information ──────────────────────────────────────────────────────
        elif name == "get_server_info":
            return await _get_server_info(guild)
        elif name == "get_channel_info":
            return await _get_channel_info(guild, args)
        elif name == "get_member_info":
            return await _get_member_info(guild, args)
        elif name == "get_role_info":
            return await _get_role_info(guild, args)
        elif name == "get_recent_messages":
            return await _get_recent_messages(guild, message, args)

        # ── Voice ────────────────────────────────────────────────────────────
        elif name == "join_voice":
            vm = ctx.get("voice_manager")
            return await _join_voice(guild, message.author, args, vm)
        elif name == "leave_voice":
            vm = ctx.get("voice_manager")
            return await _leave_voice(guild, vm)

        else:
            return {"error": f"Unknown Discord tool: {name}"}

    except discord.Forbidden as e:
        return {"error": f"Permission denied by Discord: {e}"}
    except discord.HTTPException as e:
        return {"error": f"Discord API error: {e}"}
    except Exception as e:
        logger.error(f"Discord tool '{name}' error: {e}", exc_info=True)
        return {"error": f"Unexpected error: {e}"}


# ---------------------------------------------------------------------------
# Server management implementations
# ---------------------------------------------------------------------------


async def _create_channel(
    guild: discord.Guild, bot: discord.Member, author: discord.Member, args: dict
) -> dict:
    err = _check_bot_perm(bot, "manage_channels")
    if err:
        return {"error": err}
    err = _check_user_perm(author, "manage_channels")
    if err:
        return {"error": err}

    name: str = args["name"]
    ch_type_str: str = args.get("channel_type", "text").lower()
    category_name: str | None = args.get("category")
    topic: str | None = args.get("topic")
    private: bool = args.get("private", False)
    reason: str = args.get("reason") or f"Created by {author.display_name} via Bob"

    ch_type_map = {
        "text": discord.ChannelType.text,
        "voice": discord.ChannelType.voice,
        "forum": discord.ChannelType.forum,
        "stage": discord.ChannelType.stage_voice,
    }
    ch_type = ch_type_map.get(ch_type_str, discord.ChannelType.text)

    # Resolve category
    category: discord.CategoryChannel | None = None
    if category_name:
        cat = _resolve_channel(guild, category_name)
        if isinstance(cat, discord.CategoryChannel):
            category = cat

    # Build permission overwrites for private channels
    overwrites: dict[discord.Role | discord.Member, discord.PermissionOverwrite] = {}
    if private:
        overwrites[guild.default_role] = discord.PermissionOverwrite(view_channel=False)
        overwrites[bot] = discord.PermissionOverwrite(view_channel=True)

    if ch_type == discord.ChannelType.voice:
        ch = await guild.create_voice_channel(
            name=name, category=category, overwrites=overwrites, reason=reason
        )
    elif ch_type == discord.ChannelType.forum:
        ch = await guild.create_forum(
            name=name, category=category, overwrites=overwrites, reason=reason
        )
    elif ch_type == discord.ChannelType.stage_voice:
        ch = await guild.create_stage_channel(
            name=name, category=category, overwrites=overwrites, reason=reason
        )
    else:
        kwargs: dict = dict(
            name=name, category=category, overwrites=overwrites, reason=reason
        )
        if topic:
            kwargs["topic"] = topic
        ch = await guild.create_text_channel(**kwargs)

    return {
        "success": True,
        "channel_id": ch.id,
        "channel_name": ch.name,
        "channel_type": ch_type_str,
        "category": category.name if category else None,
        "private": private,
        "result": f"Created {'private ' if private else ''}#{ch.name} ({ch_type_str})"
                  + (f" in category '{category.name}'" if category else ""),
    }


async def _delete_channel(
    guild: discord.Guild, bot: discord.Member, author: discord.Member, args: dict
) -> dict:
    err = _check_bot_perm(bot, "manage_channels")
    if err:
        return {"error": err}
    err = _check_user_perm(author, "manage_channels")
    if err:
        return {"error": err}

    ch = _resolve_channel(guild, args["channel"])
    if not ch:
        return {"error": f"Channel not found: {args['channel']}"}

    reason = args.get("reason") or f"Deleted by {author.display_name} via Bob"
    ch_name = ch.name
    await ch.delete(reason=reason)
    return {"success": True, "result": f"Deleted #{ch_name}"}


async def _rename_channel(
    guild: discord.Guild, bot: discord.Member, author: discord.Member, args: dict
) -> dict:
    err = _check_bot_perm(bot, "manage_channels")
    if err:
        return {"error": err}
    err = _check_user_perm(author, "manage_channels")
    if err:
        return {"error": err}

    ch = _resolve_channel(guild, args["channel"])
    if not ch:
        return {"error": f"Channel not found: {args['channel']}"}

    old_name = ch.name
    new_name = args["new_name"]
    reason = args.get("reason") or f"Renamed by {author.display_name} via Bob"
    await ch.edit(name=new_name, reason=reason)
    return {"success": True, "result": f"Renamed #{old_name} → #{ch.name}"}


async def _edit_channel(
    guild: discord.Guild, bot: discord.Member, author: discord.Member, args: dict
) -> dict:
    err = _check_bot_perm(bot, "manage_channels")
    if err:
        return {"error": err}
    err = _check_user_perm(author, "manage_channels")
    if err:
        return {"error": err}

    ch = _resolve_channel(guild, args["channel"])
    if not ch:
        return {"error": f"Channel not found: {args['channel']}"}

    edit_kwargs: dict = {}
    if "topic" in args and isinstance(ch, discord.TextChannel):
        edit_kwargs["topic"] = args["topic"]
    if "slowmode_delay" in args and isinstance(ch, (discord.TextChannel, discord.ForumChannel)):
        edit_kwargs["slowmode_delay"] = max(0, int(args["slowmode_delay"]))
    if "nsfw" in args and isinstance(ch, (discord.TextChannel, discord.ForumChannel)):
        edit_kwargs["nsfw"] = bool(args["nsfw"])

    reason = args.get("reason") or f"Edited by {author.display_name} via Bob"
    await ch.edit(**edit_kwargs, reason=reason)
    return {"success": True, "result": f"Updated #{ch.name} settings: {list(edit_kwargs.keys())}"}


async def _create_category(
    guild: discord.Guild, bot: discord.Member, author: discord.Member, args: dict
) -> dict:
    err = _check_bot_perm(bot, "manage_channels")
    if err:
        return {"error": err}
    err = _check_user_perm(author, "manage_channels")
    if err:
        return {"error": err}

    private = args.get("private", False)
    overwrites: dict = {}
    if private:
        overwrites[guild.default_role] = discord.PermissionOverwrite(view_channel=False)

    reason = args.get("reason") or f"Created by {author.display_name} via Bob"
    cat = await guild.create_category(args["name"], overwrites=overwrites, reason=reason)
    return {
        "success": True,
        "category_id": cat.id,
        "category_name": cat.name,
        "result": f"Created {'private ' if private else ''}category '{cat.name}'",
    }


async def _set_channel_permissions(
    guild: discord.Guild, bot: discord.Member, author: discord.Member, args: dict
) -> dict:
    err = _check_bot_perm(bot, "manage_channels")
    if err:
        return {"error": err}
    err = _check_user_perm(author, "manage_channels")
    if err:
        return {"error": err}

    ch = _resolve_channel(guild, args["channel"])
    if not ch:
        return {"error": f"Channel not found: {args['channel']}"}

    target_str = args["target"]
    target_type = args.get("target_type", "role")
    reason = args.get("reason") or f"Permissions set by {author.display_name} via Bob"

    if target_type == "role":
        if target_str.lower() in ("everyone", "@everyone", "all", "default"):
            target = guild.default_role
        else:
            target = _resolve_role(guild, target_str)
            if not target:
                return {"error": f"Role not found: {target_str}"}
    else:
        target = _resolve_member(guild, target_str)
        if not target:
            return {"error": f"Member not found: {target_str}"}

    ow = _build_perms_overwrite(args.get("allow"), args.get("deny"))
    await ch.set_permissions(target, overwrite=ow, reason=reason)

    allow_list = args.get("allow") or []
    deny_list = args.get("deny") or []
    return {
        "success": True,
        "result": (
            f"Updated #{ch.name} permissions for '{target.name}': "
            f"allowed={allow_list}, denied={deny_list}"
        ),
    }


# ---------------------------------------------------------------------------
# Role management implementations
# ---------------------------------------------------------------------------


async def _create_role(
    guild: discord.Guild, bot: discord.Member, author: discord.Member, args: dict
) -> dict:
    err = _check_bot_perm(bot, "manage_roles")
    if err:
        return {"error": err}
    err = _check_user_perm(author, "manage_roles")
    if err:
        return {"error": err}

    reason = args.get("reason") or f"Created by {author.display_name} via Bob"
    role = await guild.create_role(
        name=args["name"],
        color=_parse_color(args.get("color")),
        hoist=bool(args.get("hoist", False)),
        mentionable=bool(args.get("mentionable", False)),
        reason=reason,
    )
    return {"success": True, "role_id": role.id, "role_name": role.name,
            "result": f"Created role '{role.name}'"}


async def _delete_role(
    guild: discord.Guild, bot: discord.Member, author: discord.Member, args: dict
) -> dict:
    err = _check_bot_perm(bot, "manage_roles")
    if err:
        return {"error": err}
    err = _check_user_perm(author, "manage_roles")
    if err:
        return {"error": err}

    role = _resolve_role(guild, args["role"])
    if not role:
        return {"error": f"Role not found: {args['role']}"}
    if role.is_bot_managed() or role.is_integration() or role.is_premium_subscriber():
        return {"error": f"Can't delete '{role.name}' — it's a managed/integration role."}

    reason = args.get("reason") or f"Deleted by {author.display_name} via Bob"
    role_name = role.name
    await role.delete(reason=reason)
    return {"success": True, "result": f"Deleted role '{role_name}'"}


async def _edit_role(
    guild: discord.Guild, bot: discord.Member, author: discord.Member, args: dict
) -> dict:
    err = _check_bot_perm(bot, "manage_roles")
    if err:
        return {"error": err}
    err = _check_user_perm(author, "manage_roles")
    if err:
        return {"error": err}

    role = _resolve_role(guild, args["role"])
    if not role:
        return {"error": f"Role not found: {args['role']}"}

    edit_kwargs: dict = {}
    if "name" in args:
        edit_kwargs["name"] = args["name"]
    if "color" in args:
        edit_kwargs["color"] = _parse_color(args["color"])
    if "hoist" in args:
        edit_kwargs["hoist"] = bool(args["hoist"])
    if "mentionable" in args:
        edit_kwargs["mentionable"] = bool(args["mentionable"])

    reason = args.get("reason") or f"Edited by {author.display_name} via Bob"
    old_name = role.name
    await role.edit(**edit_kwargs, reason=reason)
    return {"success": True, "result": f"Updated role '{old_name}': {list(edit_kwargs.keys())}"}


# ---------------------------------------------------------------------------
# Member management implementations
# ---------------------------------------------------------------------------


async def _kick_member(
    guild: discord.Guild, bot: discord.Member, author: discord.Member, args: dict
) -> dict:
    err = _check_bot_perm(bot, "kick_members")
    if err:
        return {"error": err}
    err = _check_user_perm(author, "kick_members")
    if err:
        return {"error": err}

    target = _resolve_member(guild, args["member"])
    if not target:
        return {"error": f"Member not found: {args['member']}"}
    err = _check_hierarchy(bot, target)
    if err:
        return {"error": err}

    reason = args.get("reason") or f"Kicked by {author.display_name} via Bob"
    name = target.display_name
    await target.kick(reason=reason)
    return {"success": True, "result": f"Kicked {name} ({reason})"}


async def _ban_member(
    guild: discord.Guild, bot: discord.Member, author: discord.Member, args: dict
) -> dict:
    err = _check_bot_perm(bot, "ban_members")
    if err:
        return {"error": err}
    err = _check_user_perm(author, "ban_members")
    if err:
        return {"error": err}

    target = _resolve_member(guild, args["member"])
    if not target:
        return {"error": f"Member not found: {args['member']}"}
    err = _check_hierarchy(bot, target)
    if err:
        return {"error": err}

    reason = args.get("reason") or f"Banned by {author.display_name} via Bob"
    delete_days = min(max(int(args.get("delete_message_days", 0)), 0), 7)
    name = target.display_name
    await target.ban(reason=reason, delete_message_days=delete_days)
    return {"success": True, "result": f"Banned {name}" + (f" and deleted {delete_days}d of messages" if delete_days else "")}


async def _unban_member(
    guild: discord.Guild, bot: discord.Member, author: discord.Member, args: dict
) -> dict:
    err = _check_bot_perm(bot, "ban_members")
    if err:
        return {"error": err}
    err = _check_user_perm(author, "ban_members")
    if err:
        return {"error": err}

    user_str = args["user"].strip()
    # Try to find in ban list
    target_user = None
    async for ban_entry in guild.bans():
        u = ban_entry.user
        try:
            if str(u.id) == user_str or u.name.lower() == user_str.lower():
                target_user = u
                break
        except Exception:
            pass

    if not target_user:
        return {"error": f"No banned user found matching: {user_str}"}

    reason = args.get("reason") or f"Unbanned by {author.display_name} via Bob"
    await guild.unban(target_user, reason=reason)
    return {"success": True, "result": f"Unbanned {target_user.name}"}


async def _timeout_member(
    guild: discord.Guild, bot: discord.Member, author: discord.Member, args: dict
) -> dict:
    err = _check_bot_perm(bot, "moderate_members")
    if err:
        return {"error": err}
    err = _check_user_perm(author, "moderate_members")
    if err:
        return {"error": err}

    target = _resolve_member(guild, args["member"])
    if not target:
        return {"error": f"Member not found: {args['member']}"}
    err = _check_hierarchy(bot, target)
    if err:
        return {"error": err}

    duration_min = float(args["duration_minutes"])
    duration_min = min(duration_min, 40320)  # Discord max: 28 days
    until = discord.utils.utcnow() + timedelta(minutes=duration_min)
    reason = args.get("reason") or f"Timed out by {author.display_name} via Bob"
    name = target.display_name
    await target.timeout(until, reason=reason)

    # Format duration nicely
    if duration_min < 60:
        dur_str = f"{int(duration_min)} minute(s)"
    elif duration_min < 1440:
        dur_str = f"{duration_min / 60:.1f} hour(s)"
    else:
        dur_str = f"{duration_min / 1440:.1f} day(s)"

    return {"success": True, "result": f"Timed out {name} for {dur_str}. Reason: {reason}"}


async def _remove_timeout(
    guild: discord.Guild, bot: discord.Member, author: discord.Member, args: dict
) -> dict:
    err = _check_bot_perm(bot, "moderate_members")
    if err:
        return {"error": err}
    err = _check_user_perm(author, "moderate_members")
    if err:
        return {"error": err}

    target = _resolve_member(guild, args["member"])
    if not target:
        return {"error": f"Member not found: {args['member']}"}

    name = target.display_name
    await target.timeout(None)
    return {"success": True, "result": f"Removed timeout from {name}"}


async def _assign_role(
    guild: discord.Guild, bot: discord.Member, author: discord.Member, args: dict
) -> dict:
    err = _check_bot_perm(bot, "manage_roles")
    if err:
        return {"error": err}
    err = _check_user_perm(author, "manage_roles")
    if err:
        return {"error": err}

    target = _resolve_member(guild, args["member"])
    if not target:
        return {"error": f"Member not found: {args['member']}"}

    role = _resolve_role(guild, args["role"])
    if not role:
        return {"error": f"Role not found: {args['role']}"}
    if role >= bot.top_role:
        return {"error": f"Can't assign '{role.name}' — it's at or above my highest role."}

    reason = args.get("reason") or f"Role assigned by {author.display_name} via Bob"
    await target.add_roles(role, reason=reason)
    return {"success": True, "result": f"Gave '{role.name}' to {target.display_name}"}


async def _remove_role(
    guild: discord.Guild, bot: discord.Member, author: discord.Member, args: dict
) -> dict:
    err = _check_bot_perm(bot, "manage_roles")
    if err:
        return {"error": err}
    err = _check_user_perm(author, "manage_roles")
    if err:
        return {"error": err}

    target = _resolve_member(guild, args["member"])
    if not target:
        return {"error": f"Member not found: {args['member']}"}

    role = _resolve_role(guild, args["role"])
    if not role:
        return {"error": f"Role not found: {args['role']}"}
    if role >= bot.top_role:
        return {"error": f"Can't remove '{role.name}' — it's at or above my highest role."}

    reason = args.get("reason") or f"Role removed by {author.display_name} via Bob"
    await target.remove_roles(role, reason=reason)
    return {"success": True, "result": f"Removed '{role.name}' from {target.display_name}"}


async def _change_nickname(
    guild: discord.Guild, bot: discord.Member, author: discord.Member, args: dict
) -> dict:
    err = _check_bot_perm(bot, "manage_nicknames")
    if err:
        return {"error": err}
    err = _check_user_perm(author, "manage_nicknames")
    if err:
        return {"error": err}

    target = _resolve_member(guild, args["member"])
    if not target:
        return {"error": f"Member not found: {args['member']}"}
    err = _check_hierarchy(bot, target)
    if err:
        return {"error": err}

    nick = args["nickname"] or None
    old_nick = target.display_name
    await target.edit(nick=nick)
    return {
        "success": True,
        "result": f"Changed {old_nick}'s nickname to '{nick}'" if nick else f"Removed {old_nick}'s nickname",
    }


# ---------------------------------------------------------------------------
# Message management implementations
# ---------------------------------------------------------------------------


async def _send_message(
    guild: discord.Guild, bot: discord.Member, author: discord.Member, args: dict
) -> dict:
    ch = _resolve_channel(guild, args["channel"])
    if not ch:
        return {"error": f"Channel not found: {args['channel']}"}
    if not isinstance(ch, (discord.TextChannel, discord.Thread, discord.ForumChannel)):
        return {"error": f"#{ch.name} is not a text channel"}

    content = args.get("content", "")
    if not content:
        return {"error": "Message content cannot be empty"}

    msg = await ch.send(content[:2000])
    return {"success": True, "message_id": msg.id, "result": f"Sent message to #{ch.name}"}


async def _delete_message(
    guild: discord.Guild, bot: discord.Member, author: discord.Member, args: dict
) -> dict:
    err = _check_bot_perm(bot, "manage_messages")
    if err:
        return {"error": err}
    err = _check_user_perm(author, "manage_messages")
    if err:
        return {"error": err}

    ch = _resolve_channel(guild, args["channel"])
    if not ch:
        return {"error": f"Channel not found: {args['channel']}"}

    try:
        msg = await ch.fetch_message(int(args["message_id"]))
        reason = args.get("reason") or f"Deleted by {author.display_name} via Bob"
        await msg.delete()
        return {"success": True, "result": f"Deleted message {args['message_id']} from #{ch.name}"}
    except discord.NotFound:
        return {"error": "Message not found"}
    except ValueError:
        return {"error": "Invalid message ID"}


async def _bulk_delete_messages(
    guild: discord.Guild, bot: discord.Member, author: discord.Member, args: dict
) -> dict:
    err = _check_bot_perm(bot, "manage_messages")
    if err:
        return {"error": err}
    err = _check_user_perm(author, "manage_messages")
    if err:
        return {"error": err}

    ch = _resolve_channel(guild, args["channel"])
    if not ch or not isinstance(ch, (discord.TextChannel, discord.Thread)):
        return {"error": f"Text channel not found: {args['channel']}"}

    count = min(max(int(args.get("count", 10)), 1), 100)
    reason = args.get("reason") or f"Bulk delete by {author.display_name} via Bob"

    deleted = await ch.purge(limit=count, reason=reason)
    return {
        "success": True,
        "deleted_count": len(deleted),
        "result": f"Deleted {len(deleted)} messages from #{ch.name}",
    }


async def _pin_message(
    guild: discord.Guild, bot: discord.Member, author: discord.Member, args: dict
) -> dict:
    err = _check_bot_perm(bot, "manage_messages")
    if err:
        return {"error": err}

    ch = _resolve_channel(guild, args["channel"])
    if not ch:
        return {"error": f"Channel not found: {args['channel']}"}

    try:
        msg = await ch.fetch_message(int(args["message_id"]))
        await msg.pin()
        return {"success": True, "result": f"Pinned message in #{ch.name}"}
    except discord.NotFound:
        return {"error": "Message not found"}
    except ValueError:
        return {"error": "Invalid message ID"}


# ---------------------------------------------------------------------------
# Information implementations
# ---------------------------------------------------------------------------


async def _get_server_info(guild: discord.Guild) -> dict:
    text_channels = sum(1 for c in guild.channels if isinstance(c, discord.TextChannel))
    voice_channels = sum(1 for c in guild.channels if isinstance(c, discord.VoiceChannel))
    categories = len(guild.categories)

    return {
        "name": guild.name,
        "id": guild.id,
        "member_count": guild.member_count,
        "online_members": sum(1 for m in guild.members if m.status != discord.Status.offline),
        "text_channels": text_channels,
        "voice_channels": voice_channels,
        "categories": categories,
        "roles": len(guild.roles),
        "owner": str(guild.owner) if guild.owner else "Unknown",
        "created_at": guild.created_at.strftime("%Y-%m-%d"),
        "boost_level": guild.premium_tier,
        "boost_count": guild.premium_subscription_count,
    }


async def _get_channel_info(guild: discord.Guild, args: dict) -> dict:
    channel_str = args.get("channel")
    if not channel_str:
        # List all channels grouped by category
        result = {}
        for cat in guild.categories:
            result[cat.name] = [
                {"name": ch.name, "type": str(ch.type), "id": ch.id}
                for ch in cat.channels
            ]
        uncategorized = [
            {"name": ch.name, "type": str(ch.type), "id": ch.id}
            for ch in guild.channels
            if ch.category is None and not isinstance(ch, discord.CategoryChannel)
        ]
        if uncategorized:
            result["(Uncategorized)"] = uncategorized
        return {"channels": result}

    ch = _resolve_channel(guild, channel_str)
    if not ch:
        return {"error": f"Channel not found: {channel_str}"}

    info: dict = {
        "name": ch.name,
        "id": ch.id,
        "type": str(ch.type),
        "category": ch.category.name if ch.category else None,
        "created_at": ch.created_at.strftime("%Y-%m-%d"),
    }
    if isinstance(ch, discord.TextChannel):
        info["topic"] = ch.topic or ""
        info["nsfw"] = ch.is_nsfw()
        info["slowmode_delay"] = ch.slowmode_delay
    if isinstance(ch, discord.VoiceChannel):
        info["members_in_vc"] = len(ch.members)
        info["user_limit"] = ch.user_limit

    return info


async def _get_member_info(guild: discord.Guild, args: dict) -> dict:
    target = _resolve_member(guild, args["member"])
    if not target:
        return {"error": f"Member not found: {args['member']}"}

    roles = [r.name for r in reversed(target.roles) if r.name != "@everyone"]
    return {
        "username": target.name,
        "display_name": target.display_name,
        "id": target.id,
        "roles": roles,
        "top_role": target.top_role.name,
        "joined_at": target.joined_at.strftime("%Y-%m-%d") if target.joined_at else "Unknown",
        "account_created": target.created_at.strftime("%Y-%m-%d"),
        "is_timed_out": target.is_timed_out(),
        "timeout_until": str(target.timed_out_until) if target.is_timed_out() else None,
        "is_bot": target.bot,
    }


async def _get_role_info(guild: discord.Guild, args: dict) -> dict:
    role_str = args.get("role")
    if not role_str:
        roles = sorted(guild.roles, key=lambda r: r.position, reverse=True)
        return {
            "roles": [
                {"name": r.name, "id": r.id, "members": len(r.members), "color": str(r.color)}
                for r in roles
                if r.name != "@everyone"
            ]
        }

    role = _resolve_role(guild, role_str)
    if not role:
        return {"error": f"Role not found: {role_str}"}

    return {
        "name": role.name,
        "id": role.id,
        "color": str(role.color),
        "position": role.position,
        "hoist": role.hoist,
        "mentionable": role.mentionable,
        "member_count": len(role.members),
        "members": [m.display_name for m in role.members[:20]],
    }


async def _get_recent_messages(
    guild: discord.Guild, current_message: discord.Message, args: dict
) -> dict:
    channel_str = args.get("channel")
    if channel_str:
        ch = _resolve_channel(guild, channel_str)
    else:
        ch = current_message.channel

    if not ch or not isinstance(ch, (discord.TextChannel, discord.Thread)):
        return {"error": "Not a readable text channel"}

    count = min(max(int(args.get("count", 10)), 1), 50)
    messages = []
    async for msg in ch.history(limit=count):
        messages.append({
            "author": msg.author.display_name,
            "content": msg.content[:500],
            "timestamp": msg.created_at.strftime("%Y-%m-%d %H:%M UTC"),
            "message_id": str(msg.id),
        })

    return {"channel": ch.name, "messages": list(reversed(messages))}


# ---------------------------------------------------------------------------
# Voice implementations
# ---------------------------------------------------------------------------


async def _join_voice(
    guild: discord.Guild,
    author: discord.Member | discord.User,
    args: dict,
    voice_manager,
) -> dict:
    channel_str = args.get("channel")

    if channel_str:
        ch = _resolve_channel(guild, channel_str)
        if not ch or not isinstance(ch, discord.VoiceChannel):
            return {"error": f"Voice channel not found: {channel_str}"}
    else:
        # Join user's current channel
        if not isinstance(author, discord.Member) or not author.voice or not author.voice.channel:
            return {"error": "You're not in a voice channel. Specify one or join one first."}
        ch = author.voice.channel

    if voice_manager:
        return await voice_manager.join(guild, ch)

    # Fallback if no voice manager
    try:
        vc = guild.voice_client
        if vc and vc.is_connected():
            await vc.move_to(ch)
        else:
            await ch.connect()
        return {"success": True, "result": f"Joined {ch.name}"}
    except Exception as e:
        return {"error": f"Failed to join voice: {e}"}


async def _leave_voice(guild: discord.Guild, voice_manager) -> dict:
    if voice_manager:
        return await voice_manager.leave(guild)

    vc = guild.voice_client
    if vc and vc.is_connected():
        await vc.disconnect()
        return {"success": True, "result": "Left the voice channel"}
    return {"error": "Not in a voice channel"}
