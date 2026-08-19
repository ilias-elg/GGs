import aiosqlite
import time
import discord
from datetime import datetime, timezone

from .config import MONITORED_GROUPS
from .db import DB_PATH

# Deep red color for the embed
DASHBOARD_COLOR = 0x8B0000

async def build_dashboard_embed() -> discord.Embed:
    """
    Builds and returns a rich red/black Discord embed with live
    monitoring stats pulled straight from the database.
    """
    now = int(time.time())

    embed = discord.Embed(
        title="🔴  FIRE NATION — LIVE INTELLIGENCE DASHBOARD",
        color=DASHBOARD_COLOR,
        timestamp=datetime.now(timezone.utc)
    )
    embed.set_footer(text="🕒 Last updated")

    async with aiosqlite.connect(DB_PATH) as db:

        # ── SCAN HEALTH ──────────────────────────────────────────────
        async with db.execute('SELECT MAX(timestamp) FROM presence_history') as cur:
            row = await cur.fetchone()
            last_scan_ts = row[0] if row and row[0] else None

        if last_scan_ts:
            ago = now - last_scan_ts
            if ago < 60:
                scan_str = f"✅  {ago}s ago"
                stale = False
            elif ago < 300:
                scan_str = f"⚠️  {ago // 60}m ago — data may be slightly stale"
                stale = True
            else:
                scan_str = f"🔴  {ago // 60}m ago — DATA IS STALE"
                stale = True
        else:
            scan_str = "⏳  Awaiting first scan…"
            stale = True

        embed.add_field(
            name="╔═  SYSTEM STATUS",
            value=(
                f"```ansi\n"
                f"\u001b[1;31mLAST SCAN   \u001b[0m{scan_str}\n"
                f"```"
            ),
            inline=False
        )

        # ── GROUP BREAKDOWN ──────────────────────────────────────────
        group_lines = []
        total_tracked = 0
        total_online = 0
        total_ingame = 0

        for group_id, group_name in MONITORED_GROUPS.items():
            async with db.execute(
                'SELECT COUNT(*) FROM group_members WHERE group_id = ?', (group_id,)
            ) as cur:
                tracked = (await cur.fetchone())[0]
            total_tracked += tracked

            if last_scan_ts and not stale:
                # Online (presenceType 1 = online but not in game)
                async with db.execute('''
                    SELECT COUNT(DISTINCT h.user_id)
                    FROM presence_history h
                    JOIN group_members gm ON h.user_id = gm.user_id
                    WHERE gm.group_id = ? AND h.timestamp = ? AND h.presence_type = 1
                ''', (group_id, last_scan_ts)) as cur:
                    online = (await cur.fetchone())[0]

                # In-game (presenceType 2)
                async with db.execute('''
                    SELECT COUNT(DISTINCT h.user_id)
                    FROM presence_history h
                    JOIN group_members gm ON h.user_id = gm.user_id
                    WHERE gm.group_id = ? AND h.timestamp = ? AND h.presence_type = 2
                ''', (group_id, last_scan_ts)) as cur:
                    ingame = (await cur.fetchone())[0]

                total_online += online
                total_ingame += ingame
                status_str = f"{online} online  {ingame} playing"
            else:
                status_str = "— stale"

            group_lines.append(f"  {group_name:<12}  {tracked:>3} members   {status_str}")

        embed.add_field(
            name="╠═  MONITORED GROUPS",
            value=(
                f"```ansi\n"
                f"\u001b[1;31m{'GROUP':<14}{'TRACKED':>9}   ONLINE  PLAYING\u001b[0m\n"
                + "\n".join(group_lines) +
                f"\n\u001b[2;37m{'─'*48}\u001b[0m\n"
                f"  {'TOTAL':<14}{total_tracked:>3} users    {total_online:>5}   {total_ingame:>5}\n"
                f"```"
            ),
            inline=False
        )

        # ── ACTIVE GAMES ─────────────────────────────────────────────
        if last_scan_ts:
            async with db.execute('''
                SELECT h.universe_id, COUNT(DISTINCT h.user_id) as cnt, kg.name
                FROM presence_history h
                LEFT JOIN known_games kg ON h.universe_id = kg.universe_id
                WHERE h.timestamp = ?
                GROUP BY h.universe_id
                ORDER BY cnt DESC
                LIMIT 8
            ''', (last_scan_ts,)) as cur:
                game_rows = await cur.fetchall()
        else:
            game_rows = []

        if game_rows:
            total_playing = sum(r[1] for r in game_rows)
            game_lines = []
            for uid, cnt, name in game_rows:
                game_name = (name or f"Universe {uid}")[:28]
                bar_filled = int((cnt / max(total_playing, 1)) * 12)
                bar = "█" * bar_filled + "░" * (12 - bar_filled)
                game_lines.append(f"  {game_name:<28}  {cnt:>2}  [{bar}]")

            embed.add_field(
                name="╠═  TOP GAMES RIGHT NOW",
                value=(
                    f"```ansi\n"
                    f"\u001b[1;31m{'EXPERIENCE':<30}{'PLY':>4}  ACTIVITY\u001b[0m\n"
                    + "\n".join(game_lines) +
                    f"\n\u001b[2;37m{'─'*50}\u001b[0m\n"
                    f"  Total in-game: {total_playing}\n"
                    f"```"
                ),
                inline=False
            )
        else:
            embed.add_field(
                name="╠═  TOP GAMES RIGHT NOW",
                value="```\n  No members currently in-game.\n```",
                inline=False
            )

        # ── SAME SERVER CONCENTRATIONS ───────────────────────────────
        if last_scan_ts:
            async with db.execute('''
                SELECT game_id, COUNT(DISTINCT user_id) as cnt, universe_id
                FROM presence_history
                WHERE timestamp = ? AND game_id IS NOT NULL
                GROUP BY game_id
                HAVING cnt > 1
                ORDER BY cnt DESC
                LIMIT 5
            ''', (last_scan_ts,)) as cur:
                server_rows = await cur.fetchall()
        else:
            server_rows = []

        if server_rows:
            server_lines = []
            for job_id, cnt, uid in server_rows:
                async with db.execute(
                    'SELECT name FROM known_games WHERE universe_id = ?', (uid,)
                ) as cur:
                    grow = await cur.fetchone()
                gname = (grow[0] if grow else f"Universe {uid}")[:22]
                short_job = job_id[:8] + "…" if job_id and len(job_id) > 8 else job_id
                server_lines.append(f"  {gname:<24}  {cnt:>2} members  [{short_job}]")

            embed.add_field(
                name="╠═  SAME-SERVER CONCENTRATIONS",
                value=(
                    f"```ansi\n"
                    f"\u001b[1;31m{'GAME':<26}{'PLY':>4}  SERVER ID\u001b[0m\n"
                    + "\n".join(server_lines) +
                    f"\n```"
                ),
                inline=False
            )

        # ── RECENT ALERTS ────────────────────────────────────────────
        async with db.execute('''
            SELECT a.timestamp, a.alert_level, a.universe_id, kg.name
            FROM alert_history a
            LEFT JOIN known_games kg ON a.universe_id = kg.universe_id
            ORDER BY a.timestamp DESC
            LIMIT 5
        ''') as cur:
            alert_rows = await cur.fetchall()

        if alert_rows:
            level_icons = {"INFO": "🟡", "WARNING": "🟠", "HIGH": "🔴", "CRITICAL": "💀"}
            alert_lines = []
            for ts, level, uid, name in alert_rows:
                t = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%H:%M")
                icon = level_icons.get(level, "⚪")
                gname = (name or f"Universe {uid}")[:24]
                alert_lines.append(f"  {icon} {t}  [{level:<8}]  {gname}")

            embed.add_field(
                name="╠═  RECENT SPIKE ALERTS",
                value=(
                    f"```ansi\n"
                    f"\u001b[1;31m  TIME   SEVERITY    EXPERIENCE\u001b[0m\n"
                    + "\n".join(alert_lines) +
                    f"\n```"
                ),
                inline=False
            )
        else:
            embed.add_field(
                name="╠═  RECENT SPIKE ALERTS",
                value="```\n  No spike alerts recorded yet.\n```",
                inline=False
            )

        embed.add_field(
            name="╚═  INTELLIGENCE SYSTEM",
            value=(
                "```\n"
                "  Scan interval : 20 seconds\n"
                "  Group sync    : every 15 minutes\n"
                "  Groups        : TSB Air · TSB Earth · TSB Water\n"
                "```"
            ),
            inline=False
        )

    return embed
