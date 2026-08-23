"""
Roblox monitoring tools exposed to the AI.
Each function queries the live SQLite database and returns
a clean dict the AI model can reason about.
"""

import time
import aiosqlite
from roblox_monitor.db import DB_PATH
from roblox_monitor.config import MONITORED_GROUPS, SPIKE_WINDOW_SECONDS

# ---------------------------------------------------------------------------
# Tool schema definitions (OpenAI / Groq function-calling format)
# ---------------------------------------------------------------------------

TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "get_group_status",
            "description": (
                "Get live Roblox presence stats for one of the monitored groups: "
                "TSB Air, TSB Earth, or TSB Water. Returns member count, how many "
                "are currently playing Roblox, top games, and same-server info."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "group_name": {
                        "type": "string",
                        "description": "One of: 'TSB Air', 'TSB Earth', 'TSB Water'. "
                                       "Also accepts short aliases like 'Air', 'Earth', 'Water'.",
                    }
                },
                "required": ["group_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_all_group_status",
            "description": (
                "Get live presence stats for ALL three monitored groups at once "
                "(TSB Air, TSB Earth, TSB Water). Use when the user asks about "
                "overall activity or wants a comparison across groups."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_active_games",
            "description": (
                "Returns the top games currently being played by members of any "
                "monitored group, with per-group player counts."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_server_concentrations",
            "description": (
                "Returns detected same-server clusters: groups of monitored members "
                "sharing the same Roblox Job ID (i.e., actually in the same server). "
                "Only available when Roblox provides job IDs."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_recent_spikes",
            "description": (
                "Returns the most recent spike alerts detected by the monitoring system. "
                "Use when the user asks about recent unusual activity or spikes."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "limit": {
                        "type": "integer",
                        "description": "Number of recent spikes to return (default 5, max 10).",
                    }
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_spike_history",
            "description": (
                "Returns spike alert history over the past N hours. "
                "Use for trend questions like 'has Air been active today?'"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "hours": {
                        "type": "number",
                        "description": "How many hours of history to retrieve (default 24).",
                    }
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "calculate_build_stats",
            "description": (
                "Calculates exact Fire damage, speed, range, and duration stats for a character build "
                "based on their Strength stat. Use this to help users theory-craft and optimize builds."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "strength": {
                        "type": "integer",
                        "description": "The allocated Strength stat of the build.",
                    }
                },
                "required": ["strength"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_webpage",
            "description": "Fetch and read the text content of a webpage or URL. Use this when the user gives you a link (like a Trello board, Wiki, or article).",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "The full URL to fetch (e.g. https://trello.com/b/...)",
                    }
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "analyze_image",
            "description": "Analyze an image from a URL and extract text, stats, or describe it. Use this whenever the user attaches an image.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "The URL of the image to analyze."
                    },
                    "prompt": {
                        "type": "string",
                        "description": "What you want to know about the image (e.g., 'Extract all stats from this gear', 'What does this image show?')"
                    }
                },
                "required": ["url", "prompt"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "execute_discord_python",
            "description": "GOD MODE. Write and execute arbitrary Python code. You have access to `bot` (discord.ext.commands.Bot) and `message` (discord.Message). Use this to perform ANY Discord action (generating invites, DMing users, kicking, creating channels). Code MUST define an `async def main(bot, message):` function that returns a string result.",
            "parameters": {
                "type": "object",
                "properties": {
                    "code": {
                        "type": "string",
                        "description": "Python code to execute. MUST define an `async def main(bot, message):` function that returns a string. Example: 'async def main(bot, message):\\n    invite = await message.channel.create_invite()\\n    return invite.url'"
                    }
                },
                "required": ["code"],
            },
        },
    },
]

# ---------------------------------------------------------------------------
# Helper – resolve short group name to full name + ID
# ---------------------------------------------------------------------------

def _resolve_group(group_name: str) -> tuple[int, str] | tuple[None, None]:
    """Map a potentially short name like 'Air' to (group_id, 'TSB Air')."""
    name_lower = group_name.lower().strip()
    for gid, gname in MONITORED_GROUPS.items():
        if name_lower in gname.lower() or gname.lower() in name_lower:
            return gid, gname
    return None, None


async def _latest_scan_ts(db) -> int | None:
    async with db.execute("SELECT MAX(timestamp) FROM presence_history") as cur:
        row = await cur.fetchone()
        return row[0] if row and row[0] else None


async def _data_age_note(last_ts: int | None) -> str:
    if last_ts is None:
        return "No scan data yet."
    ago = int(time.time()) - last_ts
    if ago < 60:
        return f"Data is fresh ({ago}s old)."
    elif ago < 300:
        return f"Data is {ago // 60}m old — may be slightly stale."
    else:
        return f"WARNING: data is {ago // 60}m old and may be very stale."


# ---------------------------------------------------------------------------
# Tool executor
# ---------------------------------------------------------------------------

async def execute_tool(name: str, args: dict, message=None, bot=None) -> dict:
    """Dispatch a tool call by name and return a result dict."""
    try:
        if name == "get_group_status":
            return await _get_group_status(args.get("group_name", ""))
        elif name == "get_all_group_status":
            return await _get_all_group_status()
        elif name == "get_active_games":
            return await _get_active_games()
        elif name == "get_server_concentrations":
            return await _get_server_concentrations()
        elif name == "get_recent_spikes":
            return await _get_recent_spikes(int(args.get("limit", 5)))
        elif name == "get_spike_history":
            return await _get_spike_history(float(args.get("hours", 24)))
        elif name == "calculate_build_stats":
            from .build_calc import calculate_stats
            return calculate_stats(int(args.get("strength", 0)))
        elif name == "read_webpage":
            from .web_tools import read_webpage
            return await read_webpage(args.get("url", ""))
        elif name == "analyze_image":
            from .vision import analyze_image_with_vision
            return await analyze_image_with_vision(args.get("url", ""), args.get("prompt", "Describe this image in detail."))
        elif name == "execute_discord_python":
            code = args.get("code", "")
            if not code or not message or not bot:
                return {"error": "Missing code, bot, or message context."}
            
            # Strip markdown code blocks if the AI included them
            code = code.strip()
            if code.startswith("```"):
                lines = code.split("\\n")
                if lines[0].startswith("```"):
                    lines = lines[1:]
                if lines[-1].startswith("```"):
                    lines = lines[:-1]
                code = "\\n".join(lines)
                
            import traceback
            import discord
            import asyncio
            
            local_scope = {}
            # Inject discord into the environment so the AI doesn't have to import it
            exec_globals = globals().copy()
            exec_globals['discord'] = discord
            exec_globals['asyncio'] = asyncio
            
            try:
                exec(code, exec_globals, local_scope)
                if "main" not in local_scope:
                    return {"error": "Code must define an 'async def main(bot, message):' function."}
                
                result = await local_scope["main"](bot, message)
                if result is None:
                    return {"result": "Code executed successfully, but returned None."}
                return {"result": str(result)}
            except Exception as e:
                return {"error": f"Code execution failed: {type(e).__name__}: {str(e)}\\n{traceback.format_exc()}"}
        else:
            return {"error": f"Unknown tool: {name}"}
    except Exception as e:
        return {"error": str(e)}


# ---------------------------------------------------------------------------
# Individual tool implementations
# ---------------------------------------------------------------------------

async def _get_group_status(group_name: str) -> dict:
    group_id, full_name = _resolve_group(group_name)
    if group_id is None:
        return {"error": f"Unknown group: '{group_name}'. Valid groups: TSB Air, TSB Earth, TSB Water."}

    async with aiosqlite.connect(DB_PATH) as db:
        last_ts = await _latest_scan_ts(db)
        age_note = await _data_age_note(last_ts)

        # Tracked members
        async with db.execute(
            "SELECT COUNT(*) FROM group_members WHERE group_id = ?", (group_id,)
        ) as cur:
            tracked = (await cur.fetchone())[0]

        if last_ts is None:
            return {"group": full_name, "tracked": tracked, "in_game": 0,
                    "top_games": [], "note": "No scan data yet."}

        # Currently online (on Roblox but not in a game)
        async with db.execute("""
            SELECT COUNT(DISTINCT h.user_id)
            FROM presence_history h
            JOIN group_members gm ON h.user_id = gm.user_id
            WHERE gm.group_id = ? AND h.timestamp = ? AND h.presence_type = 1
        """, (group_id, last_ts)) as cur:
            online_only = (await cur.fetchone())[0]

        # Currently in-game
        async with db.execute("""
            SELECT COUNT(DISTINCT h.user_id)
            FROM presence_history h
            JOIN group_members gm ON h.user_id = gm.user_id
            WHERE gm.group_id = ? AND h.timestamp = ? AND h.presence_type = 2
        """, (group_id, last_ts)) as cur:
            in_game = (await cur.fetchone())[0]

        total_online = online_only + in_game

        # Top games
        async with db.execute("""
            SELECT h.universe_id, COUNT(DISTINCT h.user_id) as cnt, kg.name
            FROM presence_history h
            JOIN group_members gm ON h.user_id = gm.user_id
            LEFT JOIN known_games kg ON h.universe_id = kg.universe_id
            WHERE gm.group_id = ? AND h.timestamp = ?
            GROUP BY h.universe_id ORDER BY cnt DESC LIMIT 5
        """, (group_id, last_ts)) as cur:
            games = [
                {"game": row[2] or f"Universe {row[0]}", "players": row[1]}
                for row in await cur.fetchall()
            ]

        # Spike window: how many joined in last N seconds
        window_start = last_ts - SPIKE_WINDOW_SECONDS
        async with db.execute("""
            SELECT COUNT(DISTINCT h.user_id)
            FROM presence_history h
            JOIN group_members gm ON h.user_id = gm.user_id
            WHERE gm.group_id = ? AND h.timestamp >= ? AND h.timestamp <= ?
        """, (group_id, window_start, last_ts)) as cur:
            recent_window_count = (await cur.fetchone())[0]

        return {
            "group": full_name,
            "tracked_members": tracked,
            "currently_online_roblox": total_online,  # on website OR in-game
            "currently_online_only": online_only,      # on website, NOT in-game
            "currently_in_game": in_game,
            "top_games": games,
            "joined_in_last_3min": recent_window_count,
            "data_note": age_note,
        }


async def _get_all_group_status() -> dict:
    results = {}
    for gid, gname in MONITORED_GROUPS.items():
        results[gname] = await _get_group_status(gname)
    return {"groups": results}


async def _get_active_games() -> dict:
    async with aiosqlite.connect(DB_PATH) as db:
        last_ts = await _latest_scan_ts(db)
        age_note = await _data_age_note(last_ts)

        if last_ts is None:
            return {"games": [], "note": "No scan data yet."}

        async with db.execute("""
            SELECT h.universe_id, COUNT(DISTINCT h.user_id) as total, kg.name
            FROM presence_history h
            LEFT JOIN known_games kg ON h.universe_id = kg.universe_id
            WHERE h.timestamp = ?
            GROUP BY h.universe_id ORDER BY total DESC LIMIT 10
        """, (last_ts,)) as cur:
            game_rows = await cur.fetchall()

        games = []
        for uid, total, name in game_rows:
            game_entry = {
                "game": name or f"Universe {uid}",
                "total_players": total,
                "by_group": {},
            }
            for gid, gname in MONITORED_GROUPS.items():
                async with db.execute("""
                    SELECT COUNT(DISTINCT h.user_id)
                    FROM presence_history h
                    JOIN group_members gm ON h.user_id = gm.user_id
                    WHERE h.timestamp = ? AND h.universe_id = ? AND gm.group_id = ?
                """, (last_ts, uid, gid)) as cur2:
                    cnt = (await cur2.fetchone())[0]
                if cnt > 0:
                    game_entry["by_group"][gname] = cnt
            games.append(game_entry)

        return {"games": games, "data_note": age_note}


async def _get_server_concentrations() -> dict:
    async with aiosqlite.connect(DB_PATH) as db:
        last_ts = await _latest_scan_ts(db)
        age_note = await _data_age_note(last_ts)

        if last_ts is None:
            return {"concentrations": [], "note": "No scan data yet."}

        async with db.execute("""
            SELECT game_id, universe_id, COUNT(DISTINCT user_id) as cnt
            FROM presence_history
            WHERE timestamp = ? AND game_id IS NOT NULL
            GROUP BY game_id HAVING cnt > 1
            ORDER BY cnt DESC LIMIT 10
        """, (last_ts,)) as cur:
            rows = await cur.fetchall()

        if not rows:
            return {
                "concentrations": [],
                "note": "No same-server concentrations detected. "
                        "Either no one is in the same server, or Roblox did not provide job IDs.",
                "data_note": age_note,
            }

        result = []
        for job_id, uid, cnt in rows:
            async with db.execute(
                "SELECT name FROM known_games WHERE universe_id = ?", (uid,)
            ) as cur:
                row = await cur.fetchone()
            game_name = row[0] if row else f"Universe {uid}"

            by_group = {}
            for gid, gname in MONITORED_GROUPS.items():
                async with db.execute("""
                    SELECT COUNT(DISTINCT h.user_id)
                    FROM presence_history h
                    JOIN group_members gm ON h.user_id = gm.user_id
                    WHERE h.timestamp = ? AND h.game_id = ? AND gm.group_id = ?
                """, (last_ts, job_id, gid)) as cur2:
                    gcnt = (await cur2.fetchone())[0]
                if gcnt > 0:
                    by_group[gname] = gcnt

            result.append({
                "game": game_name,
                "server_id": job_id[:12] + "…" if len(job_id) > 12 else job_id,
                "total_members_in_server": cnt,
                "by_group": by_group,
            })

        return {"concentrations": result, "data_note": age_note}


async def _get_recent_spikes(limit: int = 5) -> dict:
    limit = min(max(limit, 1), 10)
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("""
            SELECT a.timestamp, a.alert_level, a.universe_id, kg.name
            FROM alert_history a
            LEFT JOIN known_games kg ON a.universe_id = kg.universe_id
            ORDER BY a.timestamp DESC LIMIT ?
        """, (limit,)) as cur:
            rows = await cur.fetchall()

    if not rows:
        return {"spikes": [], "note": "No spike alerts recorded yet."}

    from datetime import datetime, timezone
    spikes = []
    for ts, level, uid, name in rows:
        dt = datetime.fromtimestamp(ts, tz=timezone.utc)
        spikes.append({
            "time": dt.strftime("%Y-%m-%d %H:%M UTC"),
            "level": level,
            "game": name or f"Universe {uid}",
        })
    return {"spikes": spikes}


async def _get_spike_history(hours: float = 24) -> dict:
    cutoff = int(time.time()) - int(hours * 3600)
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("""
            SELECT a.timestamp, a.alert_level, a.universe_id, kg.name
            FROM alert_history a
            LEFT JOIN known_games kg ON a.universe_id = kg.universe_id
            WHERE a.timestamp >= ?
            ORDER BY a.timestamp DESC
        """, (cutoff,)) as cur:
            rows = await cur.fetchall()

    if not rows:
        return {"spikes": [], "note": f"No spike alerts in the last {hours}h."}

    from datetime import datetime, timezone
    spikes = []
    for ts, level, uid, name in rows:
        dt = datetime.fromtimestamp(ts, tz=timezone.utc)
        spikes.append({
            "time": dt.strftime("%H:%M UTC"),
            "level": level,
            "game": name or f"Universe {uid}",
        })
    return {"period_hours": hours, "spike_count": len(spikes), "spikes": spikes}
