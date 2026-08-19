import aiosqlite
import time
import discord
from datetime import datetime, timezone

from .config import MONITORED_GROUPS
from .db import DB_PATH

DASHBOARD_COLOR = 0xC0392B  # Vivid crimson

ALERT_ICONS = {"INFO": "🟡", "WARNING": "🟠", "HIGH": "🔴", "CRITICAL": "💀"}

W = 54  # target line width inside code blocks


def _bar(ratio: float, length: int = 14) -> str:
    filled = min(int(ratio * length), length)
    return "█" * filled + "░" * (length - filled)


async def _latest_ts(db) -> int | None:
    async with db.execute("SELECT MAX(timestamp) FROM presence_history") as cur:
        row = await cur.fetchone()
        return row[0] if row and row[0] else None


async def _scan_age(last_ts: int | None) -> tuple[str, bool]:
    if last_ts is None:
        return "⏳  Awaiting first scan…", True
    ago = int(time.time()) - last_ts
    if ago < 40:
        return f"✅  Live  ({ago}s ago)", False
    elif ago < 180:
        return f"⚠️  {ago}s ago — slightly stale", True
    else:
        return f"🔴  {ago // 60}m ago — DATA STALE", True


async def build_dashboard_embed() -> discord.Embed:
    now_dt = datetime.now(timezone.utc)

    embed = discord.Embed(color=DASHBOARD_COLOR, timestamp=now_dt)
    embed.set_author(name="🔴  FIRE NATION  ·  LIVE INTELLIGENCE DASHBOARD")

    async with aiosqlite.connect(DB_PATH) as db:
        last_ts = await _latest_ts(db)
        scan_str, stale = await _scan_age(last_ts)

        # ── STATUS BANNER (wide code block = forces embed width) ─────────────
        div = "═" * W
        embed.description = (
            f"```\n"
            f"  ╔{div}╗\n"
            f"  ║  {'SYSTEM STATUS':<{W-2}}║\n"
            f"  ╠{div}╣\n"
            f"  ║  LAST SCAN   {scan_str:<{W-14}}║\n"
            f"  ╚{div}╝\n"
            f"```"
        )

        # ── GROUP TABLE ──────────────────────────────────────────────────────
        totals = {"tracked": 0, "online": 0, "ingame": 0}
        group_data = {}

        for group_id, group_name in MONITORED_GROUPS.items():
            async with db.execute(
                "SELECT COUNT(*) FROM group_members WHERE group_id = ?", (group_id,)
            ) as cur:
                tracked = (await cur.fetchone())[0]

            online_only = ingame = 0
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

            totals["tracked"] += tracked
            totals["online"]  += online_only + ingame
            totals["ingame"]  += ingame
            group_data[group_name] = {
                "tracked": tracked,
                "online": online_only + ingame,
                "ingame": ingame,
            }

        div2 = "─" * W
        header = f"  {'GROUP':<16}{'TRACKED':>9}{'ONLINE':>9}{'IN-GAME':>9}"
        rows = []
        for gname, d in group_data.items():
            short = gname  # e.g. "TSB Air"
            rows.append(
                f"  {short:<16}{d['tracked']:>9}{d['online']:>9}{d['ingame']:>9}"
            )
        total_row = (
            f"  {'TOTAL':<16}{totals['tracked']:>9}"
            f"{totals['online']:>9}{totals['ingame']:>9}"
        )

        group_block = (
            f"```\n"
            f"  {div2}\n"
            f"{header}\n"
            f"  {div2}\n"
            + "\n".join(rows) + "\n"
            f"  {div2}\n"
            f"{total_row}\n"
            f"  {div2}\n"
            f"```"
        )
        embed.add_field(name="📊  MONITORED GROUPS", value=group_block, inline=False)

        # ── TOP GAMES TABLE ──────────────────────────────────────────────────
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
            MEDALS = ["#1", "#2", "#3", "#4", "#5", "#6"]
            max_cnt = game_rows[0][1] if game_rows else 1

            game_lines = []
            for i, (uid, cnt, name) in enumerate(game_rows):
                medal = MEDALS[i] if i < len(MEDALS) else f"#{i+1}"
                gname_str = (name or f"Universe {uid}")[:26]
                bar = _bar(cnt / max(max_cnt, 1), 10)

                # group breakdown
                parts = []
                for gid, grpname in MONITORED_GROUPS.items():
                    async with db.execute("""
                        SELECT COUNT(DISTINCT h.user_id)
                        FROM presence_history h JOIN group_members gm ON h.user_id = gm.user_id
                        WHERE h.timestamp=? AND h.universe_id=? AND gm.group_id=? AND h.presence_type=2
                    """, (last_ts, uid, gid)) as cur2:
                        gcnt = (await cur2.fetchone())[0]
                    if gcnt:
                        parts.append(f"{grpname.replace('TSB ', '')}: {gcnt}")

                breakdown = "  ·  ".join(parts) if parts else "—"
                game_lines.append(
                    f"  {medal:<4} {gname_str:<26}  {cnt:>3} ply  [{bar}]\n"
                    f"       ↳ {breakdown}"
                )

            game_block = (
                f"```\n"
                f"  {div2}\n"
                f"  {'#':<4} {'EXPERIENCE':<26}  {'PLY':>4}  ACTIVITY\n"
                f"  {div2}\n"
                + "\n".join(game_lines) + "\n"
                f"  {div2}\n"
                f"  Total in-game: {totals['ingame']}\n"
                f"```"
            )
            embed.add_field(name="🎮  TOP GAMES RIGHT NOW", value=game_block, inline=False)
        else:
            embed.add_field(
                name="🎮  TOP GAMES RIGHT NOW",
                value=f"```\n  {div2}\n  No members currently in-game.\n  {div2}\n```",
                inline=False,
            )

        # ── SAME-SERVER CONCENTRATIONS ───────────────────────────────────────
        if last_ts:
            async with db.execute("""
                SELECT game_id, COUNT(DISTINCT user_id) as cnt, universe_id
                FROM presence_history
                WHERE timestamp=? AND game_id IS NOT NULL AND presence_type=2
                GROUP BY game_id HAVING cnt > 1
                ORDER BY cnt DESC LIMIT 4
            """, (last_ts,)) as cur:
                server_rows = await cur.fetchall()
        else:
            server_rows = []

        if server_rows:
            srv_lines = []
            for job_id, cnt, uid in server_rows:
                async with db.execute(
                    "SELECT name FROM known_games WHERE universe_id = ?", (uid,)
                ) as cur:
                    row = await cur.fetchone()
                gname_str = (row[0] if row else f"Universe {uid}")[:24]
                parts = []
                for gid, grpname in MONITORED_GROUPS.items():
                    async with db.execute("""
                        SELECT COUNT(DISTINCT h.user_id)
                        FROM presence_history h JOIN group_members gm ON h.user_id = gm.user_id
                        WHERE h.timestamp=? AND h.game_id=? AND gm.group_id=? AND h.presence_type=2
                    """, (last_ts, job_id, gid)) as cur2:
                        gcnt = (await cur2.fetchone())[0]
                    if gcnt:
                        parts.append(f"{grpname.replace('TSB ', '')}: {gcnt}")
                breakdown = "  ·  ".join(parts) if parts else "—"
                srv_lines.append(
                    f"  {gname_str:<26}  {cnt:>3} members\n"
                    f"       ↳ {breakdown}"
                )

            srv_block = (
                f"```\n"
                f"  {div2}\n"
                f"  {'GAME':<26}  {'MEMBERS':>7}\n"
                f"  {div2}\n"
                + "\n".join(srv_lines) + "\n"
                f"  {div2}\n"
                f"```"
            )
            embed.add_field(name="🖥️  SAME-SERVER CONCENTRATIONS", value=srv_block, inline=False)

        # ── RECENT ALERTS ────────────────────────────────────────────────────
        async with db.execute("""
            SELECT a.timestamp, a.alert_level, a.universe_id, kg.name
            FROM alert_history a
            LEFT JOIN known_games kg ON a.universe_id = kg.universe_id
            ORDER BY a.timestamp DESC LIMIT 5
        """) as cur:
            alert_rows = await cur.fetchall()

        if alert_rows:
            a_lines = []
            for ts, level, uid, name in alert_rows:
                t = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%H:%M UTC")
                icon = ALERT_ICONS.get(level, "⚪")
                gname_str = (name or f"Universe {uid}")[:24]
                a_lines.append(f"  {icon}  {t}   {level:<10}  {gname_str}")

            alert_block = (
                f"```\n"
                f"  {div2}\n"
                f"  {'TIME':<12}  {'SEVERITY':<10}  EXPERIENCE\n"
                f"  {div2}\n"
                + "\n".join(a_lines) + "\n"
                f"  {div2}\n"
                f"```"
            )
            embed.add_field(name="🚨  RECENT SPIKE ALERTS", value=alert_block, inline=False)
        else:
            embed.add_field(
                name="🚨  RECENT SPIKE ALERTS",
                value=f"```\n  {div2}\n  No spike alerts recorded yet.\n  {div2}\n```",
                inline=False,
            )

        embed.set_footer(
            text="🔄 Scan every 20s  ·  🔁 Group sync every 15m  ·  Last updated"
        )

    return embed
