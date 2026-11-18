"""
Inactive-member cleanup. Anyone with no merit gained in the last N days is
warned in a DM and given a grace period; whoever still has earned nothing when
it runs out can be removed from the server.

Nothing here runs on its own: /inactivity warn and /inactivity kick are always
started by the Owner or Fire Lord, and the kick is confirmed before it happens.
Rank holders, bots and anyone who joined inside the window are never touched.
"""

import logging
from datetime import datetime, timedelta, timezone

import discord

import config

from . import merit
from .ranks import get_rank

logger = logging.getLogger("discord")

DEFAULT_DAYS = 30
DEFAULT_GRACE_DAYS = 7


async def ensure_tables() -> None:
    pool = await merit.get_pool()
    async with pool.acquire() as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS inactivity_warnings (
                guild_id text NOT NULL,
                member_id text NOT NULL,
                warned_at timestamp with time zone DEFAULT now() NOT NULL,
                deadline timestamp with time zone NOT NULL,
                dm_delivered boolean NOT NULL,
                warned_by_id text NOT NULL,
                PRIMARY KEY (guild_id, member_id)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS inactivity_exempt (
                guild_id text NOT NULL,
                member_id text NOT NULL,
                added_by_id text NOT NULL,
                PRIMARY KEY (guild_id, member_id)
            )
        """)


# ─── Who counts as inactive ───────────────────────────────────────────────────


async def _last_gain(member_ids: list[str]) -> dict[str, datetime]:
    """Most recent positive merit entry per member, across every server (merits are one shared ledger)."""
    pool = await merit.get_pool()
    async with pool.acquire() as db:
        rows = await db.fetch(
            "SELECT member_id, MAX(created_at) AS last FROM merit_awards "
            "WHERE amount > 0 AND member_id = ANY($1::text[]) GROUP BY member_id",
            member_ids,
        )
    return {r["member_id"]: r["last"] for r in rows}


async def _exempt_ids(guild_id: int) -> set[int]:
    pool = await merit.get_pool()
    async with pool.acquire() as db:
        rows = await db.fetch("SELECT member_id FROM inactivity_exempt WHERE guild_id = $1", str(guild_id))
    return {int(r["member_id"]) for r in rows}


async def set_exempt(guild_id: int, member_id: int, exempt: bool, actor_id: int) -> bool:
    """True if something changed."""
    pool = await merit.get_pool()
    async with pool.acquire() as db:
        if exempt:
            result = await db.execute(
                "INSERT INTO inactivity_exempt (guild_id, member_id, added_by_id) VALUES ($1, $2, $3) "
                "ON CONFLICT DO NOTHING",
                str(guild_id), str(member_id), str(actor_id),
            )
        else:
            result = await db.execute(
                "DELETE FROM inactivity_exempt WHERE guild_id = $1 AND member_id = $2", str(guild_id), str(member_id)
            )
    return not result.endswith(" 0")


async def list_exempt(guild_id: int) -> list[int]:
    return sorted(await _exempt_ids(guild_id))


async def find_inactive(guild: discord.Guild, days: int) -> list[discord.Member]:
    """
    Members with no merit gained in the last `days` days, oldest activity first.
    Skipped: bots, anyone holding a bot rank (HR and up), the exempt list, and
    anyone who joined less than `days` ago and hasn't had the chance to earn any.
    """
    if not guild.chunked:
        await guild.chunk()
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    exempt = await _exempt_ids(guild.id)
    candidates = [
        m for m in guild.members
        if not m.bot
        and m.id not in exempt
        and get_rank(m) == "none"
        and not (m.joined_at and m.joined_at > cutoff)
    ]
    last = await _last_gain([str(m.id) for m in candidates])
    inactive = [m for m in candidates if not (last.get(str(m.id)) and last[str(m.id)] >= cutoff)]
    inactive.sort(key=lambda m: last.get(str(m.id)) or datetime.min.replace(tzinfo=timezone.utc))
    return inactive


# ─── Warning ──────────────────────────────────────────────────────────────────


def warning_text(guild: discord.Guild, days: int, deadline: datetime) -> str:
    return (
        f"**Inactivity notice — {guild.name}**\n\n"
        f"You haven't earned any merits in the last **{days} days**. Members who stay inactive are removed "
        f"from the server.\n\n"
        f"You have until <t:{int(deadline.timestamp())}:F> (<t:{int(deadline.timestamp())}:R>) to earn a merit — "
        f"for example by joining a training, event or raid. Once you do, this warning is cancelled "
        f"and nothing happens.\n\nIf you think this is a mistake, contact HR or an Advisor."
    )


async def warn_members(
    guild: discord.Guild, members: list[discord.Member], days: int, grace_days: int, actor: discord.abc.User
) -> tuple[list[discord.Member], list[discord.Member]]:
    """DMs each member and records the warning. Returns (delivered, could_not_dm)."""
    deadline = datetime.now(timezone.utc) + timedelta(days=grace_days)
    text = warning_text(guild, days, deadline)
    delivered, failed = [], []
    pool = await merit.get_pool()
    for member in members:
        try:
            await member.send(text)
            ok = True
        except (discord.Forbidden, discord.HTTPException):
            ok = False
        (delivered if ok else failed).append(member)
        async with pool.acquire() as db:
            await db.execute(
                "INSERT INTO inactivity_warnings (guild_id, member_id, warned_at, deadline, dm_delivered, warned_by_id) "
                "VALUES ($1, $2, now(), $3, $4, $5) ON CONFLICT (guild_id, member_id) DO UPDATE SET "
                "warned_at = now(), deadline = EXCLUDED.deadline, dm_delivered = EXCLUDED.dm_delivered, "
                "warned_by_id = EXCLUDED.warned_by_id",
                str(guild.id), str(member.id), deadline, ok, str(actor.id),
            )
    logger.info(f"Inactivity warnings sent in guild {guild.id}: {len(delivered)} delivered, {len(failed)} DMs closed")
    return delivered, failed


async def clear_warnings(guild_id: int, member_id: int | None = None) -> int:
    pool = await merit.get_pool()
    async with pool.acquire() as db:
        if member_id:
            result = await db.execute(
                "DELETE FROM inactivity_warnings WHERE guild_id = $1 AND member_id = $2", str(guild_id), str(member_id)
            )
        else:
            result = await db.execute("DELETE FROM inactivity_warnings WHERE guild_id = $1", str(guild_id))
    return int(result.split()[-1])


async def pending_warnings(guild_id: int) -> list[dict]:
    pool = await merit.get_pool()
    async with pool.acquire() as db:
        rows = await db.fetch(
            "SELECT member_id, warned_at, deadline, dm_delivered FROM inactivity_warnings "
            "WHERE guild_id = $1 ORDER BY deadline", str(guild_id),
        )
    return [dict(r) for r in rows]


# ─── Removal ──────────────────────────────────────────────────────────────────


async def due_for_removal(
    guild: discord.Guild, include_undelivered: bool = False
) -> tuple[list[discord.Member], dict[str, list[discord.Member]]]:
    """
    Members whose grace period is over and who earned nothing since they were
    warned. Also returns who was skipped and why (`still_waiting`, `redeemed`,
    `no_dm`), and drops the warnings of anyone who earned a merit meanwhile.
    """
    now = datetime.now(timezone.utc)
    exempt = await _exempt_ids(guild.id)
    rows = await pending_warnings(guild.id)
    last = await _last_gain([r["member_id"] for r in rows])
    due: list[discord.Member] = []
    skipped: dict[str, list[discord.Member]] = {"still_waiting": [], "redeemed": [], "no_dm": []}
    for row in rows:
        member = guild.get_member(int(row["member_id"]))
        if member is None:
            await clear_warnings(guild.id, int(row["member_id"]))  # already gone
            continue
        gained = last.get(row["member_id"])
        if gained and gained >= row["warned_at"]:
            skipped["redeemed"].append(member)
            await clear_warnings(guild.id, member.id)
        elif member.id in exempt or member.bot or get_rank(member) != "none":
            await clear_warnings(guild.id, member.id)
        elif row["deadline"] > now:
            skipped["still_waiting"].append(member)
        elif not row["dm_delivered"] and not include_undelivered:
            skipped["no_dm"].append(member)
        else:
            due.append(member)
    return due, skipped


async def remove_members(
    guild: discord.Guild, members: list[discord.Member], actor: discord.abc.User, days: int
) -> tuple[list[discord.Member], list[tuple[discord.Member, str]]]:
    """Kicks each member. Returns (removed, [(member, why not)])."""
    removed, failed = [], []
    for member in members:
        try:
            await guild.kick(member, reason=f"Inactive: no merits gained for {days}+ days (by {actor})")
            await clear_warnings(guild.id, member.id)
            removed.append(member)
        except discord.Forbidden:
            failed.append((member, "my role is not above theirs, or I lack Kick Members"))
        except discord.HTTPException as e:
            failed.append((member, str(e)))
    return removed, failed


# ─── Audit log ────────────────────────────────────────────────────────────────


async def audit(
    bot: discord.Client, title: str, members: list[discord.Member], actor: discord.abc.User, note: str = ""
) -> None:
    channel = await merit._fetch_log_channel(bot)
    if not channel or not members:
        return
    embed = merit._audit_embed(title, f"**{len(members)}** member{'' if len(members) == 1 else 's'}. {note}".strip(), config.FIRE_RED)
    lines = [f"{m.mention} {discord.utils.escape_markdown(str(m))}" for m in members]
    for index, chunk in enumerate(merit._chunk_lines(lines)[:20]):
        embed.add_field(name="Members" if index == 0 else "​", value=chunk, inline=False)
    embed.add_field(name="Authorized by", value=merit._who(actor), inline=True)
    try:
        await channel.send(embed=embed)
    except discord.HTTPException as e:
        logger.error(f"Inactivity audit log send failed: {e}")
