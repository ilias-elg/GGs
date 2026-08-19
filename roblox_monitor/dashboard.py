import aiosqlite
import time
import discord
from datetime import datetime, timezone

from .config import MONITORED_GROUPS
from .db import DB_PATH

DASHBOARD_COLOR = 0xC0392B  # Vivid crimson red

RANK_EMOJI = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣"]
ALERT_ICONS = {"INFO": "🟡", "WARNING": "🟠", "HIGH": "🔴", "CRITICAL": "💀"}

GROUP_EMOJI = {
    "TSB Air":   "🌪️",
    "TSB Earth": "🌍",
    "TSB Water": "🌊",
}


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
                    ingame = (await cur.fetchone())[0]

                totals["online"] += online_only + ingame
                totals["ingame"] += ingame

                field_value = (
                    f"👥  **{tracked}** tracked\n"
                    f"🟢  **{online_only + ingame}** online\n"
                    f"🎮  **{ingame}** in‑game"
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
                f"{totals['ingame']} in‑game"
            ),
            inline=False,
        )

        # ── TOP GAMES ────────────────────────────────────────────────────────
        if last_ts:
            async with db.execute("""
                SELECT h.universe_id, COUNT(DISTINCT h.user_id) as cnt, kg.name
                FROM presence_history h
                LEFT JOIN known_games kg ON h.universe_id = kg.universe_id
                WHERE h.timestamp = ? AND h.presence_type = 2
                GROUP BY h.universe_id ORDER BY cnt DESC LIMIT 6
            """, (last_ts,)) as cur:
                game_rows = await cur.fetchall()
        else:
            game_rows = []

        if game_rows:
            game_lines = []
            for i, (uid, cnt, name) in enumerate(game_rows):
                rank = RANK_EMOJI[i] if i < len(RANK_EMOJI) else f"{i+1}."
                game_name = (name or f"Universe {uid}")[:32]
                # Per-group breakdown
                group_parts = []
                for gid, gname in MONITORED_GROUPS.items():
                    async with db.execute("""
                        SELECT COUNT(DISTINCT h.user_id)
                        FROM presence_history h JOIN group_members gm ON h.user_id = gm.user_id
                        WHERE h.timestamp = ? AND h.universe_id = ? AND gm.group_id = ? AND h.presence_type = 2
                    """, (last_ts, uid, gid)) as cur2:
                        gcnt = (await cur2.fetchone())[0]
                    if gcnt > 0:
                        short = gname.replace("TSB ", "")
                        group_parts.append(f"{short}: {gcnt}")

                breakdown = "  ·  ".join(group_parts) if group_parts else ""
                bar_filled = min(int((cnt / max(game_rows[0][1], 1)) * 8), 8)
                bar = "█" * bar_filled + "░" * (8 - bar_filled)

                line = f"{rank}  **{game_name}**  —  {cnt} players  `{bar}`"
                if breakdown:
                    line += f"\n> {breakdown}"
                game_lines.append(line)

            embed.add_field(
                name="🎮  Top Games Right Now",
                value="\n".join(game_lines),
                inline=False,
            )
        else:
            embed.add_field(
                name="🎮  Top Games Right Now",
                value="*No members currently in‑game.*",
                inline=False,
            )

        # ── SAME-SERVER CONCENTRATIONS ───────────────────────────────────────
        if last_ts:
            async with db.execute("""
                SELECT game_id, COUNT(DISTINCT user_id) as cnt, universe_id
                FROM presence_history
                WHERE timestamp = ? AND game_id IS NOT NULL AND presence_type = 2
                GROUP BY game_id HAVING cnt > 1
                ORDER BY cnt DESC LIMIT 3
            """, (last_ts,)) as cur:
                server_rows = await cur.fetchall()
        else:
            server_rows = []

        if server_rows:
            server_lines = []
            for job_id, cnt, uid in server_rows:
                async with db.execute(
                    "SELECT name FROM known_games WHERE universe_id = ?", (uid,)
                ) as cur:
                    row = await cur.fetchone()
                gname = (row[0] if row else f"Universe {uid}")[:28]

                group_parts = []
                for gid, grpname in MONITORED_GROUPS.items():
                    async with db.execute("""
                        SELECT COUNT(DISTINCT h.user_id)
                        FROM presence_history h JOIN group_members gm ON h.user_id = gm.user_id
                        WHERE h.timestamp = ? AND h.game_id = ? AND gm.group_id = ? AND h.presence_type = 2
                    """, (last_ts, job_id, gid)) as cur2:
                        gcnt = (await cur2.fetchone())[0]
                    if gcnt > 0:
                        short = grpname.replace("TSB ", "")
                        group_parts.append(f"{short}: {gcnt}")

                breakdown = "  ·  ".join(group_parts)
                server_lines.append(
                    f"🔗  **{gname}**  —  **{cnt}** in same server\n> {breakdown}"
                )

            embed.add_field(
                name="🖥️  Same-Server Concentrations",
                value="\n".join(server_lines),
                inline=False,
            )

        # ── RECENT ALERTS ────────────────────────────────────────────────────
        async with db.execute("""
            SELECT a.timestamp, a.alert_level, a.universe_id, kg.name
            FROM alert_history a
            LEFT JOIN known_games kg ON a.universe_id = kg.universe_id
            ORDER BY a.timestamp DESC LIMIT 4
        """) as cur:
            alert_rows = await cur.fetchall()

        if alert_rows:
            alert_lines = []
            for ts, level, uid, name in alert_rows:
                t = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%H:%M")
                icon = ALERT_ICONS.get(level, "⚪")
                gname = (name or f"Universe {uid}")[:26]
                alert_lines.append(f"{icon}  `{t}`  **{gname}**  —  {level}")

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
