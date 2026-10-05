"""
Roblox monitoring tools — ported from the original conversation/tools.py.

Removed: execute_discord_python (replaced by explicit discord_tools.py)
Kept: all Roblox monitoring + build calculator + read_webpage + analyze_image
"""

import time
import aiosqlite
from bob.features.roblox_monitor.db import DB_PATH
from bob.features.roblox_monitor.config import MONITORED_GROUPS, SPIKE_WINDOW_SECONDS

# ---------------------------------------------------------------------------
# Tool schemas (OpenAI / Groq function-calling format)
# ---------------------------------------------------------------------------

ROBLOX_SCHEMAS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "get_group_status",
            "description": (
                "Get live Roblox presence stats for one of the monitored groups: "
                "TSB Air, TSB Earth or TSB Water. Returns member count, "
                "how many are online/in-game, top games, and same-server info."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "group_name": {
                        "type": "string",
                        "description": "One of: 'TSB Air', 'TSB Earth', 'TSB Water'. "
                                       "Aliases like 'Air', 'Earth', 'Water', 'Fire' also work.",
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
                "Get live presence stats for ALL monitored groups at once. "
                "Use when the user asks about overall activity or wants a comparison."
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
            "name": "get_online_hrs",
            "description": (
                "Returns a list of High Ranks (HRs) who are currently online or in-game "
                "across all monitored groups. Use this when the user asks about HR presence."
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
                "sharing the same Roblox Job ID (i.e. actually in the same game server). "
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
                "Calculates exact Fire damage, speed, range, and duration stats for a "
                "character build based on their Strength stat. Use this to help users "
                "theory-craft and optimize builds in The Shattered Balance."
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
            "name": "find_player",
            "description": (
                "Search for a specific Roblox player by username across all monitored groups "
                "and return their current presence (online, in-game, which server)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "username": {
                        "type": "string",
                        "description": "The Roblox username or part of the username to search for.",
                    }
                },
                "required": ["username"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "send_dashboard",
            "description": (
                "Send the full FIRE NATION LIVE INTELLIGENCE DASHBOARD as a rich Discord embed. "
                "Use this ONLY when the user's current message asks for the dashboard, live stats, an "
                "overview, or a summary of all groups — never for a greeting, small talk, or a message "
                "about something else, and never just because it was sent earlier in the conversation. "
                "When it is asked for, do NOT narrate the data as text — call this tool to send the "
                "real formatted embed."
            ),
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "analyze_roblox_user",
            "description": (
                "Fetches comprehensive data for a specific Roblox user directly from Roblox APIs. "
                "Returns account creation date, friends count, followers count, group memberships, "
                "description, and whether they are banned. "
                "Use this tool to investigate specific users, verify identities, or check if an account is an alt."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "username": {
                        "type": "string",
                        "description": "The exact Roblox username to investigate.",
                    }
                },
                "required": ["username"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "query_roblox_api",
            "description": (
                "Make a raw HTTP request to any Roblox API endpoint (e.g., users.roblox.com, groups.roblox.com). "
                "Use this for dynamic lookups that aren't covered by other tools, such as searching for a user by name, "
                "checking catalog items, fetching game badges, etc."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "The full Roblox API URL.",
                    },
                    "method": {
                        "type": "string",
                        "description": "HTTP Method: 'GET' or 'POST' (default 'GET').",
                        "enum": ["GET", "POST"]
                    },
                    "json_payload": {
                        "type": "object",
                        "description": "Optional JSON payload for POST requests.",
                    }
                },
                "required": ["url"],
            },
        },
    },
]

# ---------------------------------------------------------------------------
# Set of names handled by this module
# ---------------------------------------------------------------------------

ROBLOX_TOOL_NAMES: frozenset[str] = frozenset(
    s["function"]["name"] for s in ROBLOX_SCHEMAS
)

# ---------------------------------------------------------------------------
# Helper — resolve short group name
# ---------------------------------------------------------------------------


def _resolve_group(group_name: str) -> tuple[int, str] | tuple[None, None]:
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


async def execute_roblox_tool(name: str, args: dict, ctx: dict | None = None) -> dict:
    """Dispatch a Roblox monitoring tool call."""
    try:
        if name == "get_group_status":
            return await _get_group_status(args.get("group_name", ""))
        elif name == "get_all_group_status":
            return await _get_all_group_status()
        elif name == "get_online_hrs":
            return await _get_online_hrs()
        elif name == "get_active_games":
            return await _get_active_games()
        elif name == "get_server_concentrations":
            return await _get_server_concentrations()
        elif name == "get_recent_spikes":
            return await _get_recent_spikes(int(args.get("limit", 5)))
        elif name == "get_spike_history":
            return await _get_spike_history(float(args.get("hours", 24)))
        elif name == "calculate_build_stats":
            from bob.features.roblox_monitor.build_calc import calculate_stats
            return calculate_stats(int(args.get("strength", 0)))
        elif name == "send_dashboard":
            return await _execute_send_dashboard(ctx or {})
        elif name == "find_player":
            return await _find_player(args.get("username", ""))
        elif name == "analyze_roblox_user":
            return await _analyze_roblox_user(args.get("username", ""))
        elif name == "query_roblox_api":
            return await _query_roblox_api(args.get("url", ""), args.get("method", "GET"), args.get("json_payload", None))
        else:
            return {"error": f"Unknown Roblox tool: {name}"}
    except Exception as e:
        return {"error": str(e)}


# ---------------------------------------------------------------------------
# Implementations
# ---------------------------------------------------------------------------

from bob.features.roblox_monitor.dashboard import HR_THRESHOLDS


def _unnamed_game(universe_id) -> str:
    """Rows with no universe are members who are online outside a game, or in a game their privacy settings hide."""
    return "Online, not in a game or game hidden" if universe_id is None else f"Universe {universe_id}"

async def _get_online_hrs() -> dict:
    async with aiosqlite.connect(DB_PATH) as db:
        last_ts = await _latest_scan_ts(db)
        if last_ts is None:
            return {"hrs": [], "note": "No scan data yet."}

        # 1. Fetch memberships and deduplicate
        async with db.execute("SELECT user_id, group_id, rank, username, role FROM group_members") as cur:
            all_memberships = await cur.fetchall()
            
        user_best_group = {}
        for uid, gid, rank, username, role in all_memberships:
            if uid not in user_best_group or rank > user_best_group[uid]['rank']:
                user_best_group[uid] = {'gid': gid, 'rank': rank, 'username': username, 'roles': []}
                
        # Collect roles
        for uid, gid, rank, username, role in all_memberships:
            if uid in user_best_group:
                user_best_group[uid]['roles'].append((gid, role, rank))

        # 2. Fetch presence
        async with db.execute('''
            SELECT user_id, presence_type, game_id 
            FROM presence_history 
            WHERE timestamp = ?
        ''', (last_ts,)) as cur:
            presences = await cur.fetchall()
            
        hrs_online = []
        for uid, ptype, game_id in presences:
            if uid in user_best_group:
                data = user_best_group[uid]
                gid = data['gid']
                rank = data['rank']
                username = data['username'] or str(uid)
                
                threshold = HR_THRESHOLDS.get(gid, 999)
                if rank >= threshold:
                    # They are an HR in their main group
                    hr_roles = [r[1] for r in data['roles'] if r[2] >= HR_THRESHOLDS.get(r[0], 999)]
                    status = "In-game" if ptype == 2 else "Online"
                    hrs_online.append({
                        "username": username,
                        "main_group": MONITORED_GROUPS.get(gid, str(gid)),
                        "hr_roles": hr_roles,
                        "status": status,
                        "job_id": game_id
                    })
                    
        return {"online_hrs": hrs_online, "total": len(hrs_online), "note": await _data_age_note(last_ts)}


async def _get_group_status(group_name: str) -> dict:
    group_id, full_name = _resolve_group(group_name)
    if group_id is None:
        return {
            "error": f"Unknown group: '{group_name}'. Valid groups: TSB Air, TSB Earth, TSB Water (TSB Fire is not tracked)."
        }

    async with aiosqlite.connect(DB_PATH) as db:
        last_ts = await _latest_scan_ts(db)
        age_note = await _data_age_note(last_ts)

        async with db.execute(
            "SELECT COUNT(*) FROM group_members WHERE group_id = ?", (group_id,)
        ) as cur:
            tracked = (await cur.fetchone())[0]

        if last_ts is None:
            return {
                "group": full_name, "tracked": tracked,
                "in_game": 0, "top_games": [], "note": "No scan data yet.",
            }

        async with db.execute("""
            SELECT COUNT(DISTINCT h.user_id)
            FROM presence_history h JOIN group_members gm ON h.user_id = gm.user_id
            WHERE gm.group_id = ? AND h.timestamp = ? AND h.presence_type = 1
        """, (group_id, last_ts)) as cur:
            online_only = (await cur.fetchone())[0]

        async with db.execute("""
            SELECT COUNT(DISTINCT h.user_id)
            FROM presence_history h JOIN group_members gm ON h.user_id = gm.user_id
            WHERE gm.group_id = ? AND h.timestamp = ? AND h.presence_type = 2
        """, (group_id, last_ts)) as cur:
            in_game = (await cur.fetchone())[0]

        async with db.execute("""
            SELECT h.universe_id, COUNT(DISTINCT h.user_id) as cnt, kg.name
            FROM presence_history h JOIN group_members gm ON h.user_id = gm.user_id
            LEFT JOIN known_games kg ON h.universe_id = kg.universe_id
            WHERE gm.group_id = ? AND h.timestamp = ?
            GROUP BY h.universe_id ORDER BY cnt DESC LIMIT 5
        """, (group_id, last_ts)) as cur:
            games = [
                {"game": row[2] or _unnamed_game(row[0]), "players": row[1]}
                for row in await cur.fetchall()
            ]

        window_start = last_ts - SPIKE_WINDOW_SECONDS
        async with db.execute("""
            SELECT COUNT(DISTINCT h.user_id)
            FROM presence_history h JOIN group_members gm ON h.user_id = gm.user_id
            WHERE gm.group_id = ? AND h.timestamp >= ? AND h.timestamp <= ?
        """, (group_id, window_start, last_ts)) as cur:
            recent_window_count = (await cur.fetchone())[0]

        return {
            "group": full_name,
            "tracked_members": tracked,
            "currently_online_roblox": online_only + in_game,
            "currently_online_only": online_only,
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
            FROM presence_history h LEFT JOIN known_games kg ON h.universe_id = kg.universe_id
            WHERE h.timestamp = ?
            GROUP BY h.universe_id ORDER BY total DESC LIMIT 10
        """, (last_ts,)) as cur:
            game_rows = await cur.fetchall()

        games = []
        for uid, total, name in game_rows:
            game_entry = {"game": name or _unnamed_game(uid), "total_players": total, "by_group": {}}
            for gid, gname in MONITORED_GROUPS.items():
                async with db.execute("""
                    SELECT COUNT(DISTINCT h.user_id)
                    FROM presence_history h JOIN group_members gm ON h.user_id = gm.user_id
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
                "note": "No same-server concentrations detected.",
                "data_note": age_note,
            }

        result = []
        for job_id, uid, cnt in rows:
            async with db.execute(
                "SELECT name FROM known_games WHERE universe_id = ?", (uid,)
            ) as cur:
                row = await cur.fetchone()
            game_name = row[0] if row else f"Universe {uid}"

            by_group: dict = {}
            for gid, gname in MONITORED_GROUPS.items():
                async with db.execute("""
                    SELECT COUNT(DISTINCT h.user_id)
                    FROM presence_history h JOIN group_members gm ON h.user_id = gm.user_id
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
            FROM alert_history a LEFT JOIN known_games kg ON a.universe_id = kg.universe_id
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
            FROM alert_history a LEFT JOIN known_games kg ON a.universe_id = kg.universe_id
            WHERE a.timestamp >= ? ORDER BY a.timestamp DESC
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


# ---------------------------------------------------------------------------
# Find Player Tool
# ---------------------------------------------------------------------------

async def _find_player(username: str) -> dict:
    if not username:
        return {"error": "Username is required"}
    
    async with aiosqlite.connect(DB_PATH) as db:
        last_ts = await _latest_scan_ts(db)
        age_note = await _data_age_note(last_ts)
        
        query = """
            SELECT user_id, group_id, rank, role, username
            FROM group_members
            WHERE username LIKE ? OR CAST(user_id AS TEXT) = ?
        """
        async with db.execute(query, (f"%{username}%", username)) as cur:
            matches = await cur.fetchall()
            
        if not matches:
            return {"status": f"No player matching '{username}' found in any monitored group.", "note": age_note}
            
        results = []
        for uid, gid, rank, role, uname in matches:
            group_name = MONITORED_GROUPS.get(gid, f"Group {gid}")
            player_info = {
                "username": uname or str(uid),
                "user_id": uid,
                "group": group_name,
                "rank": rank,
                "role": role,
                "status": "Offline / Unknown"
            }
            
            if last_ts:
                async with db.execute("""
                    SELECT presence_type, universe_id, game_id
                    FROM presence_history
                    WHERE timestamp = ? AND user_id = ?
                """, (last_ts, uid)) as pcur:
                    prow = await pcur.fetchone()
                    
                if prow:
                    ptype, u_id, g_id = prow
                    if ptype == 1:
                        player_info["status"] = "Online (Website/App)"
                    elif ptype == 2:
                        from bob.features.roblox_monitor.config import TARGET_UNIVERSE_ID
                        if u_id == TARGET_UNIVERSE_ID:
                            if g_id:
                                if "-" in g_id:
                                    parts = g_id.split("-")
                                    short_id = f"{parts[1]}-{parts[2]}" if len(parts) >= 3 else g_id
                                else:
                                    short_id = g_id
                                player_info["status"] = f"In-game (The Shattered Balance) - Server ID: {short_id}"
                            else:
                                player_info["status"] = "In-game (The Shattered Balance) - Unassigned Server"
                        elif u_id is None:
                            player_info["status"] = "In-game (which game is hidden by their privacy settings)"
                        else:
                            async with db.execute("SELECT name FROM known_games WHERE universe_id = ?", (u_id,)) as gcur:
                                grow = await gcur.fetchone()
                                game_name = grow[0] if grow else f"Universe {u_id}"
                            player_info["status"] = f"In-game ({game_name})"
                            
            results.append(player_info)
            
    return {"matches": results, "note": age_note}


# ---------------------------------------------------------------------------
# Dashboard — sends the full rich Discord embed directly to the channel
# ---------------------------------------------------------------------------


async def _execute_send_dashboard(ctx: dict) -> dict:
    """Build and send the dashboard embed directly to the channel."""
    try:
        from bob.features.roblox_monitor.dashboard import build_dashboard_embed
        from bob.features.roblox_monitor.db import DB_PATH
        import aiosqlite
        embed = await build_dashboard_embed()
        message = ctx.get("message")
        if message and message.channel:
            sent_msg = await message.channel.send(embed=embed)
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute("INSERT OR REPLACE INTO bot_status (key, value) VALUES ('live_dash_channel', ?)", (str(sent_msg.channel.id),))
                await db.execute("INSERT OR REPLACE INTO bot_status (key, value) VALUES ('live_dash_msg', ?)", (str(sent_msg.id),))
                await db.commit()
            return {"sent": True}
        return {"sent": False, "error": "No channel available"}
    except Exception as e:
        return {"sent": False, "error": str(e)}


# ---------------------------------------------------------------------------
# Analyze Roblox User Tool
# ---------------------------------------------------------------------------

async def _analyze_roblox_user(username: str) -> dict:
    import aiohttp
    import asyncio
    if not username:
        return {"error": "Username is required"}
    
    async with aiohttp.ClientSession() as session:
        # 1. Resolve username to user ID
        async with session.post("https://users.roblox.com/v1/usernames/users", json={"usernames": [username]}) as resp:
            data = await resp.json()
            if not data.get("data"):
                return {"error": f"Roblox user '{username}' does not exist."}
            user_info = data["data"][0]
            user_id = user_info["id"]
            display_name = user_info["displayName"]
            
        # 2. Fetch all details concurrently
        async def fetch(url):
            async with session.get(url) as r:
                return await r.json() if r.status == 200 else None

        info, friends, followers, followings, groups = await asyncio.gather(
            fetch(f"https://users.roblox.com/v1/users/{user_id}"),
            fetch(f"https://friends.roblox.com/v1/users/{user_id}/friends/count"),
            fetch(f"https://friends.roblox.com/v1/users/{user_id}/followers/count"),
            fetch(f"https://friends.roblox.com/v1/users/{user_id}/followings/count"),
            fetch(f"https://groups.roblox.com/v1/users/{user_id}/groups/roles")
        )

        result = {
            "user_id": user_id,
            "username": username,
            "display_name": display_name,
            "profile_url": f"https://www.roblox.com/users/{user_id}/profile",
        }
        
        if info:
            result["created_at"] = info.get("created")
            result["description"] = info.get("description")
            result["is_banned"] = info.get("isBanned")
            
        if friends: result["friends_count"] = friends.get("count")
        if followers: result["followers_count"] = followers.get("count")
        if followings: result["followings_count"] = followings.get("count")
        
        if groups and "data" in groups:
            result["group_count"] = len(groups["data"])
            result["groups"] = [g["group"]["name"] for g in groups["data"]]
            
        return result


# ---------------------------------------------------------------------------
# Generic Roblox API Query Tool
# ---------------------------------------------------------------------------

async def _query_roblox_api(url: str, method: str = "GET", json_payload: dict = None) -> dict:
    import aiohttp
    
    if not url.startswith("https://") or "roblox.com" not in url:
        return {"error": "Invalid URL. Must be a valid roblox.com HTTPS endpoint."}
        
    try:
        async with aiohttp.ClientSession() as session:
            if method.upper() == "POST":
                async with session.post(url, json=json_payload, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if "application/json" in resp.headers.get("Content-Type", ""):
                        return {"status": resp.status, "data": await resp.json()}
                    else:
                        return {"status": resp.status, "text": await resp.text()}
            else:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if "application/json" in resp.headers.get("Content-Type", ""):
                        return {"status": resp.status, "data": await resp.json()}
                    else:
                        return {"status": resp.status, "text": await resp.text()}
    except Exception as e:
        return {"error": str(e)}
