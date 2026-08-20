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

async def _get_public_job_ids() -> set:
    """Fetches up to 100 public servers to check if a job ID is public."""
    url = f"https://games.roblox.com/v1/games/{TARGET_PLACE_ID}/servers/Public?limit=100"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return {s["id"] for s in data.get("data", []) if "id" in s}
    except Exception:
        pass
    return set()


async def _latest_ts(db) -> int | None:
    async with db.execute("SELECT MAX(timestamp) FROM presence_history") as cur:
        row = await cur.fetchone()
        return row[0] if row and row[0] else None


async def _scan_age(last_ts: int | None) -> tuple[str, bool]:
    """Returns (status_string, is_stale)."""
    if last_ts is None:
        return "⏳  Awaiting first scan…", True
    ago = int(time.time()) - last_ts
    if ago < 40:
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

    async with aiosqlite.connect(DB_PATH) as db:
        last_ts = await _latest_ts(db)
        scan_str, stale = await _scan_age(last_ts)

        # ── SYSTEM STATUS bar (description) ─────────────────────────────────
        embed.description = (
            f"```\n"
            f"  LAST SCAN   {scan_str}\n"
            f"```"
        )

        # ── PER-GROUP INLINE FIELDS (3 across) ──────────────────────────────
        totals = {"tracked": 0, "online": 0, "ingame": 0}

        for group_id, group_name in MONITORED_GROUPS.items():
            emoji = GROUP_EMOJI.get(group_name, "●")

            async with db.execute(
                "SELECT COUNT(*) FROM group_members WHERE group_id = ?", (group_id,)
            ) as cur:
                tracked = (await cur.fetchone())[0]
            totals["tracked"] += tracked

            if last_ts and not stale:
                # Online (anywhere on Roblox)
                async with db.execute("""
                    SELECT COUNT(DISTINCT h.user_id)
                    FROM presence_history h JOIN group_members gm ON h.user_id = gm.user_id
                    WHERE gm.group_id = ? AND h.timestamp = ? AND h.presence_type = 1
                """, (group_id, last_ts)) as cur:
                    online_only = (await cur.fetchone())[0]

                # In-game (only in TARGET_UNIVERSE_ID)
                async with db.execute("""
                    SELECT COUNT(DISTINCT h.user_id)
                    FROM presence_history h JOIN group_members gm ON h.user_id = gm.user_id
                    WHERE gm.group_id = ? AND h.timestamp = ? AND h.presence_type = 2 AND h.universe_id = ?
                """, (group_id, last_ts, TARGET_UNIVERSE_ID)) as cur:
                    ingame = (await cur.fetchone())[0]

                totals["online"] += online_only + ingame
                totals["ingame"] += ingame

                field_value = (
                    f"👥  **{tracked}** tracked\n"
                    f"🟢  **{online_only + ingame}** online\n"
                    f"🎮  **{ingame}** playing"
                )
            else:
                field_value = (
                    f"👥  **{tracked}** tracked\n"
                    f"🟡  Data stale"
                )

            embed.add_field(
                name=f"{emoji}  {group_name}",
                value=field_value,
                inline=True,
            )

        # Totals row (full width)
        embed.add_field(
            name="\u200b",  # zero-width space — blank separator
            value=(
                f"**Total:**  {totals['tracked']} tracked  ·  "
                f"{totals['online']} online  ·  "
                f"{totals['ingame']} playing **The Shattered Balance**"
            ),
            inline=False,
        )

        # ── ACTIVE SERVERS (TARGET GAME ONLY) ────────────────────────────────
        if last_ts:
            async with db.execute("""
                SELECT game_id, COUNT(DISTINCT user_id) as cnt
                FROM presence_history
                WHERE timestamp = ? AND game_id IS NOT NULL AND presence_type = 2 AND universe_id = ?
                GROUP BY game_id
                ORDER BY cnt DESC LIMIT 5
            """, (last_ts, TARGET_UNIVERSE_ID)) as cur:
                server_rows = await cur.fetchall()
        else:
            server_rows = []

        if server_rows:
            server_lines = []
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
                
                # Format Job ID to match in-game format (first 8 chars split by hyphen)
                # E.g. "520eba8d-..." -> "520e-ba8d"
                if len(job_id) >= 8:
                    short_id = f"{job_id[:4]}-{job_id[4:8]}"
                else:
                    short_id = job_id
                    
                is_private = job_id not in public_jobs if public_jobs else False
                server_label = "Private Server" if is_private else "Public Server"
                
                line = f"🔗  **{cnt}** members in server `ID: {short_id}` ({server_label})\n> {breakdown}"
                
                if hrs_dict:
                    hr_strings = [f"**{uname}** ({'/'.join(groups)})" for uname, groups in hrs_dict.items()]
                    line += f"\n> ⚠️ **HRs Present:** {', '.join(hr_strings)}"
                    
                server_lines.append(line)

            embed.add_field(
                name="🖥️  Active Servers (The Shattered Balance)",
                value="\n\n".join(server_lines),
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
            text="🔄 Scan every 20s  ·  🔁 Group sync every 15m  ·  Last updated"
        )

    return embed
