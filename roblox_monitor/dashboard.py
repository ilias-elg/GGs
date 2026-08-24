import aiosqlite
import time
import discord
import aiohttp
from datetime import datetime, timezone

from .config import MONITORED_GROUPS, TARGET_UNIVERSE_ID, TARGET_PLACE_ID
from .db import DB_PATH

DASHBOARD_COLOR = 0xC0392B  # Vivid crimson red

ALERT_ICONS = {"INFO": "🟡", "WARNING": "🟠", "HIGH": "🔴", "CRITICAL": "💀"}

GROUP_EMOJI = {
    "TSB Air":   "🌪️",
    "TSB Earth": "🌍",
    "TSB Water": "🌊",
    "TSB Fire":  "🔥",
}

HR_THRESHOLDS = {
    485588074: 5,   # Air: Monk
    592750791: 6,   # Earth: Lieutenant
    1029776236: 6,  # Water: Lieutenant
    44315578: 8     # Fire: Lieutenant
}

async def _get_public_job_ids() -> dict:
    """Fetches up to 500 public servers to get total player counts."""
    url = f"https://games.roblox.com/v1/games/{TARGET_PLACE_ID}/servers/Public?limit=100"
    results = {}
    cursor = ""
    try:
        async with aiohttp.ClientSession() as session:
            for _ in range(10):
                page_url = url if not cursor else f"{url}&cursor={cursor}"
                async with session.get(page_url) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        for s in data.get("data", []):
                            if "id" in s:
                                results[s["id"]] = s.get("playing", 0)
                        
                        cursor = data.get("nextPageCursor")
                        if not cursor:
                            break
                    else:
                        break
    except Exception:
        pass
    return results


async def _latest_ts(db) -> int | None:
    try:
        async with db.execute("SELECT value FROM bot_status WHERE key = 'last_scan_time'") as cur:
            row = await cur.fetchone()
            if row and row[0]:
                return int(row[0])
    except Exception:
        pass
        
    # Fallback if bot_status fails or doesn't exist yet
    async with db.execute("SELECT MAX(timestamp) FROM presence_history") as cur:
        row = await cur.fetchone()
        return row[0] if row and row[0] else None


async def _scan_age(last_ts: int | None) -> tuple[str, bool]:
    """Returns (status_string, is_stale)."""
    if last_ts is None:
        return "⏳  Awaiting first scan…", True
    ago = int(time.time()) - last_ts
    if ago < 60:
        return f"✅  {ago}s ago", False
    elif ago < 180:
        return f"⚠️  {ago}s ago — slightly stale", True
    else:
        mins = ago // 60
        return f"🔴  {mins}m ago — DATA STALE", True


async def build_dashboard_embed() -> discord.Embed:
    now_dt = datetime.now(timezone.utc)

    embed = discord.Embed(
        color=DASHBOARD_COLOR,
        timestamp=now_dt,
    )

    embed.set_author(
        name="🔴  FIRE NATION  ·  LIVE INTELLIGENCE DASHBOARD",
    )

    async with aiosqlite.connect(DB_PATH, timeout=15.0) as db:
        last_ts = await _latest_ts(db)
        scan_str, stale = await _scan_age(last_ts)

        # ── SYSTEM STATUS bar (description) ─────────────────────────────────
        embed.description = (
            f"```\n"
            f"  LAST SCAN   {scan_str}\n"
            f"```"
        )

        # ── PER-GROUP INLINE FIELDS (3 across) ──────────────────────────────
        group_stats = {
            gid: {"tracked": 0, "online_only": 0, "other_game": 0, "ingame": 0, "unassigned": 0}
            for gid in MONITORED_GROUPS
        }
        totals = {"tracked": 0, "online": 0, "ingame": 0, "other_game": 0, "unassigned": 0}

        # 1. Deduplicate Tracked Members (assign to highest rank group)
        async with db.execute("SELECT user_id, group_id, rank FROM group_members") as cur:
            all_memberships = await cur.fetchall()
            
        user_best_group = {}
        for uid, gid, rank in all_memberships:
            if uid not in user_best_group or rank > user_best_group[uid]['rank']:
                user_best_group[uid] = {'gid': gid, 'rank': rank}
                
        for uid, data in user_best_group.items():
            gid = data['gid']
            if gid in group_stats:
                group_stats[gid]["tracked"] += 1
                totals["tracked"] += 1

        # 2. Fetch and Deduplicate Presence
        if last_ts:
            async with db.execute('''
                SELECT user_id, presence_type, universe_id, game_id 
                FROM presence_history 
                WHERE timestamp = ?
            ''', (last_ts,)) as cur:
                presences = await cur.fetchall()
                
            for uid, ptype, universe_id, game_id in presences:
                if uid in user_best_group:
                    gid = user_best_group[uid]['gid']
                    if gid not in group_stats:
                        continue
                        
                    if ptype == 1:
                        group_stats[gid]["online_only"] += 1
                        totals["online"] += 1
                    elif ptype == 2:
                        if universe_id == TARGET_UNIVERSE_ID:
                            group_stats[gid]["ingame"] += 1
                            totals["ingame"] += 1
                            totals["online"] += 1
                            if game_id is None:
                                group_stats[gid]["unassigned"] += 1
                                totals["unassigned"] += 1
                        else:
                            group_stats[gid]["other_game"] += 1
                            totals["other_game"] += 1
                            totals["online"] += 1

        for group_id, group_name in MONITORED_GROUPS.items():
            emoji = GROUP_EMOJI.get(group_name, "●")
            stats = group_stats[group_id]

            if last_ts:
                total_online = stats["online_only"] + stats["other_game"] + stats["ingame"]
                
                field_value = (
                    f"👥  **{stats['tracked']}** tracked\n"
                    f"🟢  **{total_online}** online\n"
                    f"🎮  **{stats['ingame']}** playing"
                )
                
                extras = []
                if stats["other_game"] > 0:
                    extras.append(f"{stats['other_game']} in other games")
                if stats["unassigned"] > 0:
                    extras.append(f"{stats['unassigned']} unassigned server")
                    
                if extras:
                    field_value += f"\n> *{', '.join(extras)}*"
            else:
                field_value = (
                    f"👥  **{stats['tracked']}** tracked\n"
                    f"🟡  Data stale"
                )

            embed.add_field(
                name=f"{emoji}  {group_name}",
                value=field_value,
                inline=True,
            )

        # Totals row (full width)
        total_extras = []
        if totals["other_game"] > 0:
            total_extras.append(f"{totals['other_game']} in other games")
        if totals["unassigned"] > 0:
            total_extras.append(f"{totals['unassigned']} unassigned server")
            
        totals_text = (
            f"**Total:**  {totals['tracked']} tracked  ·  "
            f"{totals['online']} online  ·  "
            f"{totals['ingame']} playing **The Shattered Balance**"
        )
        if total_extras:
            totals_text += f"\n*({', '.join(total_extras)})*"
            
        embed.add_field(
            name="\u200b",  # zero-width space — blank separator
            value=totals_text,
            inline=False,
        )

        # ── ACTIVE SERVERS (TARGET GAME ONLY) ────────────────────────────────
        if last_ts:
            async with db.execute("""
                SELECT game_id, COUNT(DISTINCT user_id) as cnt
                FROM presence_history
                WHERE timestamp = ? AND game_id IS NOT NULL AND presence_type = 2 AND universe_id = ?
                GROUP BY game_id
                ORDER BY cnt DESC LIMIT 15
            """, (last_ts, TARGET_UNIVERSE_ID)) as cur:
                server_rows = await cur.fetchall()
        else:
            server_rows = []

        if server_rows:
            public_server_lines = []
            private_server_lines = []
            public_jobs = await _get_public_job_ids()
            
            for job_id, cnt in server_rows:
                # Fetch ALL group memberships for users in this server
                async with db.execute("""
                    SELECT h.user_id, gm.group_id, gm.rank, gm.username
                    FROM presence_history h JOIN group_members gm ON h.user_id = gm.user_id
                    WHERE h.timestamp = ? AND h.game_id = ? AND h.presence_type = 2
                """, (last_ts, job_id)) as cur2:
                    all_memberships = await cur2.fetchall()
                
                # Deduplicate users: assign to the group where they have the highest rank
                user_best_group = {}
                hrs_dict = {}
                
                for uid, gid, rank, username in all_memberships:
                    if uid not in user_best_group or rank > user_best_group[uid]['rank']:
                        user_best_group[uid] = {'gid': gid, 'rank': rank}
                        
                    # Also collect HR info (list all HR roles they hold)
                    threshold = HR_THRESHOLDS.get(gid, 999)
                    if rank > threshold:
                        uname = username or str(uid)
                        if uname not in hrs_dict:
                            hrs_dict[uname] = []
                        short_name = MONITORED_GROUPS[gid].replace("TSB ", "")
                        hrs_dict[uname].append(short_name)
                        
                # Tally unique counts per group based on their highest ranked group
                group_tally = {}
                for uid, data in user_best_group.items():
                    gid = data['gid']
                    group_tally[gid] = group_tally.get(gid, 0) + 1
                    
                group_parts = []
                for gid, grpname in MONITORED_GROUPS.items():
                    if gid in group_tally:
                        short = grpname.replace("TSB ", "")
                        group_parts.append(f"{short}: {group_tally[gid]}")

                breakdown = "  ·  ".join(group_parts)
                
                # The game and extension use the 2nd and 3rd blocks of the UUID
                # e.g. "2d8f3baf-7104-4802-..." -> "7104-4802"
                if "-" in job_id:
                    parts = job_id.split("-")
                    if len(parts) >= 3:
                        short_id = f"{parts[1]}-{parts[2]}"
                    else:
                        short_id = job_id
                else:
                    short_id = job_id
                    
                is_private = job_id not in public_jobs if public_jobs else False
                
                if not is_private and job_id in public_jobs:
                    total_players = public_jobs[job_id]
                    line = f"🔗  **{total_players} players** in server `ID: {short_id}`\n> 🛡️ **{cnt} group members:** {breakdown}"
                else:
                    line = f"🔗  **{cnt} tracked members** in server `ID: {short_id}`\n> 🛡️ **Breakdown:** {breakdown}"
                
                if hrs_dict:
                    hr_strings = [f"**{uname}** ({'/'.join(groups)})" for uname, groups in hrs_dict.items()]
                    line += f"\n> ⚠️ **HRs Present:** {', '.join(hr_strings)}"
                    
                if is_private:
                    private_server_lines.append(line)
                else:
                    public_server_lines.append(line)

            if public_server_lines:
                embed.add_field(
                    name="🖥️  Active Public Servers",
                    value="\n\n".join(public_server_lines),
                    inline=False,
                )
            if private_server_lines:
                embed.add_field(
                    name="🔒  Active Private Servers",
                    value="\n\n".join(private_server_lines),
                    inline=False,
                )
        else:
            embed.add_field(
                name="🖥️  Active Servers (The Shattered Balance)",
                value="*No members currently in-game.*",
                inline=False,
            )

        # ── RECENT ALERTS (TARGET GAME ONLY) ─────────────────────────────────
        async with db.execute("""
            SELECT timestamp, alert_level
            FROM alert_history
            WHERE universe_id = ?
            ORDER BY timestamp DESC LIMIT 4
        """, (TARGET_UNIVERSE_ID,)) as cur:
            alert_rows = await cur.fetchall()

        if alert_rows:
            alert_lines = []
            for ts, level in alert_rows:
                t = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%H:%M")
                icon = ALERT_ICONS.get(level, "⚪")
                alert_lines.append(f"{icon}  `{t}`  —  {level} SPIKE")

            embed.add_field(
                name="🚨  Recent Spike Alerts",
                value="\n".join(alert_lines),
                inline=False,
            )
        else:
            embed.add_field(
                name="🚨  Recent Spike Alerts",
                value="*No spike alerts recorded yet.*",
                inline=False,
            )

        # ── FOOTER ───────────────────────────────────────────────────────────
        embed.set_footer(
            text="🔄 Dash updates every 20s  ·  📡 Scan every 20s  ·  🔁 Group sync every 5m  ·  Last updated"
        )

    return embed
