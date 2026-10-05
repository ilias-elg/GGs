import itertools
import logging
import time
from datetime import datetime, timezone

import aiohttp
import aiosqlite
import discord

from .config import MONITORED_GROUPS, ROBLOX_PROXY_LIST, TARGET_UNIVERSE_ID, TARGET_PLACE_ID
from .db import DB_PATH

logger = logging.getLogger('discord')

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

MAX_SERVERS_SHOWN = 15

# ── Public server list ────────────────────────────────────────────────────────
# Refreshed once per presence scan, each time through the next proxy, so any
# one IP asks only every few minutes. Asking on every redraw from the host's
# own IP got it refused, and every server was then shown as public. A list
# that could not be refreshed is reused for a while; with no list at all the
# servers are shown without claiming to know which kind they are.

PUBLIC_SERVERS_REFRESH_SECONDS = 20
PUBLIC_SERVERS_MAX_AGE_SECONDS = 10 * 60

_public_servers: dict | None = None
_public_servers_at = 0.0
_public_servers_tried_at = 0.0
_proxy_cycle = itertools.cycle(ROBLOX_PROXY_LIST) if ROBLOX_PROXY_LIST else None


async def _fetch_public_servers() -> dict | None:
    """{server id: players in it} for every public server, or None unless the whole list was read."""
    url = f"https://games.roblox.com/v1/games/{TARGET_PLACE_ID}/servers/Public"
    proxy = next(_proxy_cycle) if _proxy_cycle else None
    results = {}
    cursor = ""
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
            for _ in range(10):
                params = {"limit": 100, **({"cursor": cursor} if cursor else {})}
                async with session.get(url, params=params, proxy=proxy) as resp:
                    if resp.status != 200:
                        return None
                    data = await resp.json()
                for s in data.get("data", []):
                    if "id" in s:
                        results[s["id"]] = s.get("playing", 0)
                cursor = data.get("nextPageCursor")
                if not cursor:
                    return results
    except Exception:
        return None
    return results


async def _get_public_job_ids() -> dict | None:
    """The public server list, refreshed every 20 seconds while Roblox is answering; None when it isn't known."""
    global _public_servers, _public_servers_at, _public_servers_tried_at
    now = time.monotonic()
    if now - _public_servers_tried_at >= PUBLIC_SERVERS_REFRESH_SECONDS or _public_servers_tried_at == 0:
        _public_servers_tried_at = now
        fresh = await _fetch_public_servers()
        if fresh is not None:
            _public_servers, _public_servers_at = fresh, now
    if _public_servers is not None and now - _public_servers_at > PUBLIC_SERVERS_MAX_AGE_SECONDS:
        _public_servers = None
    return _public_servers


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


async def _coverage_line(db) -> str:
    """How much of the roster the last scan re-checked — a scan can only refresh what the rate limit allows."""
    try:
        async with db.execute("SELECT value FROM bot_status WHERE key = 'last_scan_coverage'") as cur:
            row = await cur.fetchone()
        refreshed, total, oldest = (int(x) for x in row[0].split("/"))
    except Exception:
        return ""
    if refreshed >= total:
        return "  COVERAGE    every member checked\n"
    return f"  COVERAGE    {refreshed}/{total} re-checked · oldest {oldest}s\n"


def _short_server_id(job_id: str) -> str:
    # The game and extension use the 2nd and 3rd blocks of the UUID
    # e.g. "2d8f3baf-7104-4802-..." -> "7104-4802"
    parts = job_id.split("-")
    return f"{parts[1]}-{parts[2]}" if len(parts) >= 3 else job_id


def _plural(count: int, word: str) -> str:
    return f"{count} {word}{'' if count == 1 else 's'}"


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
            f"{await _coverage_line(db) if last_ts else ''}"
            f"```"
        )

        # ── Everything below is computed from these two reads ───────────────
        async with db.execute("SELECT user_id, group_id, rank, username FROM group_members") as cur:
            all_memberships = await cur.fetchall()

        presences = []
        if last_ts:
            async with db.execute('''
                SELECT user_id, presence_type, universe_id, game_id
                FROM presence_history
                WHERE timestamp = ?
            ''', (last_ts,)) as cur:
                presences = await cur.fetchall()

        # A member of several groups is counted once, in the group where
        # their rank is highest.
        user_best_group = {}
        usernames = {}
        hr_groups = {}  # user_id → short names of the groups they are HR in
        for uid, gid, rank, username in all_memberships:
            if gid not in MONITORED_GROUPS:
                continue
            if uid not in user_best_group or rank > user_best_group[uid]['rank']:
                user_best_group[uid] = {'gid': gid, 'rank': rank}
            if username:
                usernames[uid] = username
            if rank > HR_THRESHOLDS.get(gid, 999):
                hr_groups.setdefault(uid, []).append(MONITORED_GROUPS[gid].replace("TSB ", ""))

        # ── PER-GROUP INLINE FIELDS (3 across) ──────────────────────────────
        group_stats = {
            gid: {"tracked": 0, "online_only": 0, "other_game": 0, "hidden_game": 0, "ingame": 0, "unassigned": 0}
            for gid in MONITORED_GROUPS
        }
        totals = {"tracked": 0, "online": 0, "ingame": 0, "other_game": 0, "hidden_game": 0, "unassigned": 0}

        for data in user_best_group.values():
            group_stats[data['gid']]["tracked"] += 1
            totals["tracked"] += 1

        servers = {}  # server id → user ids of tracked members in it
        seen = set()
        for uid, ptype, universe_id, game_id in presences:
            if uid not in user_best_group or uid in seen or ptype not in (1, 2):
                continue
            seen.add(uid)
            stats = group_stats[user_best_group[uid]['gid']]
            totals["online"] += 1

            if ptype == 1:
                stats["online_only"] += 1
            elif universe_id == TARGET_UNIVERSE_ID:
                stats["ingame"] += 1
                totals["ingame"] += 1
                if game_id is None:
                    stats["unassigned"] += 1
                    totals["unassigned"] += 1
                else:
                    servers.setdefault(game_id, []).append(uid)
            elif universe_id is None:
                # In a game, but their privacy settings hide which one.
                stats["hidden_game"] += 1
                totals["hidden_game"] += 1
            else:
                stats["other_game"] += 1
                totals["other_game"] += 1

        def extras_for(stats) -> list[str]:
            extras = []
            if stats["other_game"] > 0:
                extras.append(f"{stats['other_game']} in other games")
            if stats["hidden_game"] > 0:
                extras.append(f"{stats['hidden_game']} in a hidden game")
            if stats["unassigned"] > 0:
                extras.append(f"{stats['unassigned']} unassigned server")
            return extras

        for group_id, group_name in MONITORED_GROUPS.items():
            emoji = GROUP_EMOJI.get(group_name, "●")
            stats = group_stats[group_id]

            if last_ts:
                total_online = (
                    stats["online_only"] + stats["other_game"] + stats["hidden_game"] + stats["ingame"]
                )

                field_value = (
                    f"👥  **{stats['tracked']}** tracked\n"
                    f"🟢  **{total_online}** online\n"
                    f"🎮  **{stats['ingame']}** playing"
                )
                for extra in extras_for(stats):
                    field_value += f"\n> *{extra}*"
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
        totals_text = (
            f"**Total:**  {totals['tracked']} tracked  ·  "
            f"{totals['online']} online  ·  "
            f"{totals['ingame']} playing **The Shattered Balance**"
        )
        total_extras = extras_for(totals)
        if total_extras:
            totals_text += f"\n*({'  ·  '.join(total_extras)})*"

        embed.add_field(
            name="​",  # zero-width space — blank separator
            value=totals_text,
            inline=False,
        )

        # ── ACTIVE SERVERS (TARGET GAME ONLY) ────────────────────────────────
        if servers:
            public_jobs = await _get_public_job_ids()
            ranked = sorted(servers.items(), key=lambda item: (-len(item[1]), item[0]))
            shown, hidden = ranked[:MAX_SERVERS_SHOWN], ranked[MAX_SERVERS_SHOWN:]

            public_server_lines = []
            private_server_lines = []
            unknown_server_lines = []

            for job_id, members in shown:
                cnt = len(members)
                group_tally = {}
                for uid in members:
                    gid = user_best_group[uid]['gid']
                    group_tally[gid] = group_tally.get(gid, 0) + 1
                breakdown = "  ·  ".join(
                    f"{grpname.replace('TSB ', '')}: {group_tally[gid]}"
                    for gid, grpname in MONITORED_GROUPS.items() if gid in group_tally
                )

                short_id = _short_server_id(job_id)
                if public_jobs is not None and job_id in public_jobs:
                    line = (
                        f"🔗  **{_plural(public_jobs[job_id], 'player')}** in server `ID: {short_id}`\n"
                        f"> 🛡️ **{_plural(cnt, 'group member')}:** {breakdown}"
                    )
                else:
                    line = (
                        f"🔗  **{_plural(cnt, 'tracked member')}** in server `ID: {short_id}`\n"
                        f"> 🛡️ **Breakdown:** {breakdown}"
                    )

                hrs = [
                    f"**{usernames.get(uid, uid)}** ({'/'.join(hr_groups[uid])})"
                    for uid in members if uid in hr_groups
                ]
                if hrs:
                    line += f"\n> ⚠️ **HRs Present:** {', '.join(hrs)}"

                if public_jobs is None:
                    unknown_server_lines.append(line)
                elif job_id in public_jobs:
                    public_server_lines.append(line)
                else:
                    private_server_lines.append(line)

            def add_chunked_fields(title, lines):
                current_chunk = []
                current_len = 0
                part = 1
                for line in lines:
                    if current_len + len(line) + 2 > 1000:
                        name = title if part == 1 else f"{title} (Part {part})"
                        embed.add_field(name=name, value="\n\n".join(current_chunk), inline=False)
                        current_chunk = [line]
                        current_len = len(line)
                        part += 1
                    else:
                        current_chunk.append(line)
                        current_len += len(line) + 2

                if current_chunk:
                    name = title if part == 1 else f"{title} (Part {part})"
                    embed.add_field(name=name, value="\n\n".join(current_chunk), inline=False)

            if hidden:
                more = (
                    f"*…and {_plural(len(hidden), 'more server')} with "
                    f"{_plural(sum(len(m) for _, m in hidden), 'tracked member')}*"
                )
                (unknown_server_lines or private_server_lines or public_server_lines).append(more)

            if public_server_lines:
                add_chunked_fields("🖥️  Active Public Servers", public_server_lines)
            if private_server_lines:
                add_chunked_fields("🔒  Active Private Servers", private_server_lines)
            if unknown_server_lines:
                add_chunked_fields("🖥️  Active Servers (public / private not known right now)", unknown_server_lines)
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
                icon = ALERT_ICONS.get(level, "⚪")
                alert_lines.append(f"{icon}  <t:{ts}:t>  —  {level} SPIKE  (<t:{ts}:R>)")

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
            text="🔄 Dash updates every 15s  ·  📡 Scan every 20s  ·  🔁 Group sync every 5m  ·  Last updated"
        )

    return embed
