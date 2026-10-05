"""
Merit business logic — the single place rank rules, DB writes, and audit
logging live. Both the slash commands (cog.py) and the AI chat tools
(bob/tools/fire_nation_tools.py) call into this file rather than each
re-implementing the rules, so a rule only ever needs to change in one place.
"""

import logging
from datetime import datetime, timezone
from decimal import Decimal
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import asyncpg
import discord

import config

from .ranks import RANK_ORDER, is_protected_owner

logger = logging.getLogger("discord")

MERIT_TYPES = ("exam", "event", "raid", "bonus")


class MeritError(Exception):
    pass


def fmt_amount(amount: float) -> str:
    """1.0 → "1", 0.5 → "0.5" — merits are stored to one decimal place."""
    return f"{round(amount, 1):g}"


def plural(amount: float) -> str:
    return "" if amount == 1 else "s"


def fixed_merit_amount(merit_type: str) -> float:
    """exam/event = 1, raid = 3. Bonus has no fixed amount — it's user-supplied."""
    return 3 if merit_type == "raid" else 1


# Exams and events only: a co-host earns this on top of the participant merit
# they get for being pinged in the announcement.
COHOST_BONUS_AMOUNT = 0.5
COHOST_MERIT_TYPES = ("exam", "event")


def assert_can_award(actor_rank: str, merit_type: str) -> None:
    if merit_type not in MERIT_TYPES:
        raise MeritError(f"Unknown merit type '{merit_type}'.")
    if RANK_ORDER[actor_rank] < RANK_ORDER["hr"]:
        raise MeritError("Access Denied — HR and above only.")
    if merit_type in ("raid", "bonus") and RANK_ORDER[actor_rank] < RANK_ORDER["advisor"]:
        raise MeritError("Only Advisors and above can award Raid or Bonus merits.")


def assert_can_remove(actor_rank: str) -> None:
    if RANK_ORDER[actor_rank] < RANK_ORDER["advisor"]:
        raise MeritError("Access Denied — Advisor and above only.")


def assert_can_view_history(actor_rank: str) -> None:
    if RANK_ORDER[actor_rank] < RANK_ORDER["hr"]:
        raise MeritError("Access Denied — HR and above only.")


def assert_can_manage_data(actor_rank: str) -> None:
    if actor_rank not in ("owner", "second"):
        raise MeritError("Access Denied — only the Owner or Fire Lord can reset system data.")


def assert_not_protected_owner(actor_rank: str, target_id: int) -> None:
    """The Fire Lord (second in command) cannot award/remove merits affecting the Owner."""
    if is_protected_owner(actor_rank, target_id):
        raise MeritError("Fire Lord cannot award or remove merits that affect the Owner.")


def assert_valid_amount(amount: float | None) -> None:
    """Bonus awards and removals both use the same 0.1–50 range."""
    if not amount or amount < 0.1 or amount > 50:
        raise MeritError("Amount must be between 0.1 and 50.")


# ─── Database ─────────────────────────────────────────────────────────────────
# Postgres, with the same merit_awards table Jarvis uses — so both bots can
# share one database, and Jarvis's existing ledger carries straight over.

_pool: asyncpg.Pool | None = None


def _dsn() -> str:
    """DATABASE_URL minus libpq-only options that asyncpg would pass to the server as settings."""
    parts = urlsplit(config.DATABASE_URL)
    query = [(k, v) for k, v in parse_qsl(parts.query) if k != "channel_binding"]
    return urlunsplit(parts._replace(query=urlencode(query)))


async def get_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        if not config.DATABASE_URL:
            raise MeritError("DATABASE_URL is not set, so the merit database is unavailable.")
        _pool = await asyncpg.create_pool(
            _dsn(),
            min_size=0,
            max_size=5,
            # Hosted Postgres (Neon, Supabase) drops idle connections and often
            # sits behind a pooler that can't keep prepared statements.
            max_inactive_connection_lifetime=60,
            statement_cache_size=0,
            timeout=15,
        )
    return _pool


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


async def ensure_merit_tables() -> None:
    pool = await get_pool()
    async with pool.acquire() as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS merit_awards (
                id serial PRIMARY KEY NOT NULL,
                guild_id text NOT NULL,
                member_id text NOT NULL,
                member_tag text NOT NULL,
                amount numeric(4, 1) NOT NULL,
                proof_url text NOT NULL,
                awarded_by_id text NOT NULL,
                awarded_by_tag text NOT NULL,
                created_at timestamp with time zone DEFAULT now() NOT NULL
            )
        """)
        await db.execute(
            "CREATE INDEX IF NOT EXISTS merit_awards_guild_member_idx ON merit_awards (guild_id, member_id)"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS merit_awards_guild_created_idx ON merit_awards (guild_id, created_at)"
        )
        # Where the rows of members who left the home server go, so the daily
        # cleanup never destroys anything.
        await db.execute("""
            CREATE TABLE IF NOT EXISTS merit_awards_archive (
                id integer NOT NULL,
                guild_id text NOT NULL,
                member_id text NOT NULL,
                member_tag text NOT NULL,
                amount numeric(4, 1) NOT NULL,
                proof_url text NOT NULL,
                awarded_by_id text NOT NULL,
                awarded_by_tag text NOT NULL,
                created_at timestamp with time zone NOT NULL,
                archived_at timestamp with time zone DEFAULT now() NOT NULL
            )
        """)


_INSERT = (
    "INSERT INTO merit_awards (guild_id, member_id, member_tag, amount, proof_url, "
    "awarded_by_id, awarded_by_tag) VALUES ($1, $2, $3, $4, $5, $6, $7)"
)


def _numeric(amount: float) -> Decimal:
    return Decimal(str(round(amount, 1)))


async def record_award(
    guild_id: int,
    recipients: list[discord.Member],
    amount: float,
    proof_url: str,
    actor: discord.abc.User,
) -> None:
    await record_awards(guild_id, [(m, amount) for m in recipients], proof_url, actor)


async def record_awards(
    guild_id: int,
    awards: list[tuple[discord.Member, float]],
    proof_url: str,
    actor: discord.abc.User,
) -> None:
    """Records (member, amount) pairs that belong to one action — e.g. participants plus a co-host."""
    logger.info(
        f"Recording merit award: {len(awards)} entries totalling "
        f"+{fmt_amount(sum(a for _, a in awards))} in guild {guild_id} by {actor.id}"
    )
    pool = await get_pool()
    async with pool.acquire() as db:
        # One transaction, so an award to several people is all-or-nothing.
        async with db.transaction():
            await db.executemany(
                _INSERT,
                [
                    (str(guild_id), str(m.id), str(m), _numeric(amount), proof_url, str(actor.id), str(actor))
                    for m, amount in awards
                ],
            )


async def record_removal(
    guild_id: int,
    target: discord.Member,
    amount: float,
    reason: str,
    actor: discord.abc.User,
) -> None:
    logger.info(f"Recording merit removal: -{amount} for {target.id} in guild {guild_id}")
    pool = await get_pool()
    async with pool.acquire() as db:
        await db.execute(
            _INSERT,
            str(guild_id), str(target.id), str(target), _numeric(-amount), reason, str(actor.id), str(actor),
        )


async def get_member_total(member_id: int) -> float:
    """Merit is one shared ledger across every server the bot is in — not filtered by guild."""
    pool = await get_pool()
    async with pool.acquire() as db:
        total = await db.fetchval(
            "SELECT COALESCE(SUM(amount), 0) FROM merit_awards WHERE member_id = $1", str(member_id)
        )
    return round(float(total), 1)


async def get_leaderboard(limit: int | None = None) -> list[dict]:
    # Grouping by member_id alone (and taking the most recently recorded tag)
    # avoids splitting one person into two leaderboard lines whenever they
    # change their Discord username.
    query = (
        "SELECT member_id, (array_agg(member_tag ORDER BY created_at DESC))[1] AS member_tag, "
        "SUM(amount) AS total FROM merit_awards GROUP BY member_id ORDER BY SUM(amount) DESC"
    )
    pool = await get_pool()
    async with pool.acquire() as db:
        rows = await (db.fetch(query + " LIMIT $1", int(limit)) if limit else db.fetch(query))
    return [
        {"member_id": int(r["member_id"]), "member_tag": r["member_tag"], "total": round(float(r["total"]), 1)}
        for r in rows
    ]


async def get_member_history(member_id: int) -> list[dict]:
    pool = await get_pool()
    async with pool.acquire() as db:
        rows = await db.fetch(
            "SELECT amount, proof_url, created_at FROM merit_awards WHERE member_id = $1 "
            "ORDER BY created_at DESC, id DESC",
            str(member_id),
        )
    return [
        {"amount": float(r["amount"]), "proof_url": r["proof_url"], "created_at": int(r["created_at"].timestamp())}
        for r in rows
    ]


async def get_recent_actions(limit: int, awarded_by_id: int | None = None) -> list[dict]:
    """
    The latest ledger rows, newest first — optionally only those authorized by
    one person. Lets Bob check what really got written instead of trusting
    his own memory of the conversation.
    """
    columns = "SELECT member_tag, amount, proof_url, awarded_by_tag, created_at FROM merit_awards"
    order = " ORDER BY created_at DESC, id DESC LIMIT "
    pool = await get_pool()
    async with pool.acquire() as db:
        if awarded_by_id:
            rows = await db.fetch(
                columns + " WHERE awarded_by_id = $1" + order + "$2", str(awarded_by_id), int(limit)
            )
        else:
            rows = await db.fetch(columns + order + "$1", int(limit))
    return [
        {
            "member_tag": r["member_tag"],
            "amount": float(r["amount"]),
            "proof_url": r["proof_url"],
            "awarded_by_tag": r["awarded_by_tag"],
            "created_at": int(r["created_at"].timestamp()),
        }
        for r in rows
    ]


async def proof_recorded(proof_url: str) -> bool:
    """True when something was already recorded against this proof link."""
    pool = await get_pool()
    async with pool.acquire() as db:
        return await db.fetchval("SELECT EXISTS (SELECT 1 FROM merit_awards WHERE proof_url = $1)", proof_url)


async def count_entries() -> int:
    pool = await get_pool()
    async with pool.acquire() as db:
        return await db.fetchval("SELECT COUNT(*) FROM merit_awards")


async def archive_members(member_ids: list[int]) -> None:
    """Moves every ledger row of these members into merit_awards_archive, taking them off the leaderboard."""
    ids = [str(i) for i in member_ids]
    pool = await get_pool()
    async with pool.acquire() as db:
        async with db.transaction():
            await db.execute(
                "INSERT INTO merit_awards_archive (id, guild_id, member_id, member_tag, amount, proof_url, "
                "awarded_by_id, awarded_by_tag, created_at) "
                "SELECT id, guild_id, member_id, member_tag, amount, proof_url, awarded_by_id, awarded_by_tag, "
                "created_at FROM merit_awards WHERE member_id = ANY($1::text[])",
                ids,
            )
            await db.execute("DELETE FROM merit_awards WHERE member_id = ANY($1::text[])", ids)
    logger.info(f"Archived the merit records of {len(ids)} departed member(s)")


async def prune_departed_members(bot: discord.Client) -> list[dict]:
    """
    Takes everyone who is no longer in the home server off the leaderboard and
    returns their final leaderboard entries. Does nothing when the member list
    can't be trusted — a wrong "not here" would wipe someone's merits.
    """
    guild = bot.get_guild(config.MERIT_HOME_GUILD_ID)
    if guild is None or guild.unavailable:
        logger.warning(f"Merit cleanup skipped — home server {config.MERIT_HOME_GUILD_ID} is not available")
        return []
    if not guild.chunked:
        await guild.chunk()

    departed = []
    for entry in await get_leaderboard():
        if guild.get_member(entry["member_id"]):
            continue
        # The cache says they're gone; ask Discord directly before acting on it.
        try:
            await guild.fetch_member(entry["member_id"])
        except discord.NotFound:
            departed.append(entry)
        except discord.HTTPException as e:
            logger.warning(f"Merit cleanup could not check member {entry['member_id']}: {e}")
    if departed:
        await archive_members([e["member_id"] for e in departed])
    return departed


async def reset_all_data(guild_id: int) -> list[dict]:
    """
    Deletes every merit record (all servers) and returns the final leaderboard
    as a backup. Callers are responsible for confirming with the user first.
    """
    entries = await get_leaderboard()
    pool = await get_pool()
    async with pool.acquire() as db:
        await db.execute("DELETE FROM merit_awards")
    logger.info(f"Merit data reset from guild {guild_id} — {len(entries)} members exported")
    return entries


# ─── Audit logging — every merit action is logged here, and only here ────────


async def _fetch_log_channel(bot: discord.Client):
    if not config.OWNER_LOG_CHANNEL_ID:
        logger.warning("No DISCORD_OWNER_LOG_CHANNEL_ID configured — action was not logged")
        return None
    channel = bot.get_channel(config.OWNER_LOG_CHANNEL_ID)
    if channel is None:
        try:
            channel = await bot.fetch_channel(config.OWNER_LOG_CHANNEL_ID)
        except discord.HTTPException:
            channel = None
    if channel is None or not hasattr(channel, "send"):
        logger.warning(f"Owner log channel {config.OWNER_LOG_CHANNEL_ID} not found or not writable")
        return None
    return channel


def _audit_embed(title: str, description: str, color: int) -> discord.Embed:
    embed = discord.Embed(
        title=title,
        description=description,
        color=color,
        timestamp=datetime.now(timezone.utc),
    )
    embed.set_footer(text="FIRE NATION • OWNER AUDIT CHANNEL")
    return embed


def _who(user: discord.abc.User) -> str:
    """Mention plus username: the mention is clickable, the username survives if the mention can't resolve."""
    return f"<@{user.id}> {discord.utils.escape_markdown(str(user))}"


def _chunk_lines(lines: list[str], limit: int = 1024) -> list[str]:
    """Packs lines into as few embed-field values as fit Discord's per-field limit."""
    chunks, current = [], ""
    for line in lines:
        if current and len(current) + 1 + len(line) > limit:
            chunks.append(current)
            current = line
        else:
            current = f"{current}\n{line}" if current else line
    if current:
        chunks.append(current)
    return chunks


async def audit_award(
    bot: discord.Client,
    awards: list[tuple[discord.Member, float]],
    merit_type: str,
    actor: discord.abc.User,
    proof_url: str | None = None,
    host: discord.Member | None = None,
    cohost: discord.Member | None = None,
    reason: str | None = None,
) -> None:
    """One embed per action: who got how much in total, with the host and co-host marked."""
    channel = await _fetch_log_channel(bot)
    if not channel:
        return

    totals: dict[int, float] = {}
    members: dict[int, discord.Member] = {}
    for member, amount in awards:
        totals[member.id] = totals.get(member.id, 0) + amount
        members[member.id] = member
    host_id, cohost_id = getattr(host, "id", None), getattr(cohost, "id", None)

    def line(member_id: int) -> str:
        role = " · **host**" if member_id == host_id else " · **co-host**" if member_id == cohost_id else ""
        return f"`+{fmt_amount(totals[member_id]):<3}` {_who(members[member_id])}{role}"

    # Host first, then co-host, then everyone else by name.
    order = sorted(
        totals,
        key=lambda i: (i != host_id, i != cohost_id, str(members[i]).lower()),
    )
    grand_total = sum(totals.values())
    count = len(totals)
    embed = _audit_embed(
        f"{merit_type} merits recorded",
        f"**{count}** member{'' if count == 1 else 's'} · "
        f"**{fmt_amount(grand_total)}** merit{plural(grand_total)} in total",
        config.FIRE_ORANGE,
    )
    for index, chunk in enumerate(_chunk_lines([line(i) for i in order])[:20]):
        embed.add_field(name="Recipients" if index == 0 else "​", value=chunk, inline=False)
    if reason:
        embed.add_field(name="Reason", value=reason[:1024], inline=False)
    embed.add_field(name="Authorized by", value=_who(actor), inline=True)
    if proof_url:
        embed.add_field(name="Proof", value=f"[Open the message]({proof_url})", inline=True)

    try:
        await channel.send(embed=embed)
        # Ping @everyone when a Bonus of more than 3 is awarded — flags it for owner review
        largest = max(totals.values())
        if merit_type == "Bonus" and largest > 3:
            await channel.send(
                f"@everyone — **{discord.utils.escape_markdown(str(actor))}** has awarded a "
                f"**+{fmt_amount(largest)} Bonus**. Owner review requested.",
                allowed_mentions=discord.AllowedMentions(everyone=True),
            )
    except discord.HTTPException as e:
        logger.error(f"Merit award audit log send failed: {e}")


async def audit_removal(
    bot: discord.Client,
    target: discord.Member,
    amount: float,
    reason: str,
    actor: discord.abc.User,
) -> None:
    channel = await _fetch_log_channel(bot)
    if not channel:
        return

    embed = _audit_embed(
        "Merits removed",
        f"`-{fmt_amount(amount):<3}` {_who(target)}",
        config.FIRE_RED,
    )
    embed.add_field(name="Reason", value=reason[:1024], inline=False)
    embed.add_field(name="Authorized by", value=_who(actor), inline=True)
    try:
        await channel.send(embed=embed)
    except discord.HTTPException as e:
        logger.error(f"Merit removal audit log send failed: {e}")


async def audit_prune(bot: discord.Client, departed: list[dict]) -> None:
    channel = await _fetch_log_channel(bot)
    if not channel:
        return
    count = len(departed)
    embed = _audit_embed(
        "Departed members taken off the leaderboard",
        f"**{count}** member{'' if count == 1 else 's'} no longer in the server · records archived",
        config.FIRE_RED,
    )
    lines = [
        f"`{fmt_amount(e['total']):<4}` <@{e['member_id']}> {discord.utils.escape_markdown(e['member_tag'])}"
        for e in departed
    ]
    for index, chunk in enumerate(_chunk_lines(lines)[:20]):
        embed.add_field(name="Final totals" if index == 0 else "​", value=chunk, inline=False)
    try:
        await channel.send(embed=embed)
    except discord.HTTPException as e:
        logger.error(f"Merit cleanup audit log send failed: {e}")


async def audit_reset(
    bot: discord.Client, entries: list[dict], actor: discord.abc.User
) -> discord.Embed:
    """Posts the pre-reset backup embed to the owner log channel and returns it."""
    backup_lines = "\n".join(
        f"`[ID: {e['member_id']}]` **#{i}** {e['member_tag']} — **{fmt_amount(e['total'])}** merits"
        for i, e in enumerate(entries, start=1)
    ) or "No data recorded prior to reset."

    embed = discord.Embed(
        title="FIRE NATION // SYSTEM DATA BACKUP & RESET EXPORT",
        description=f"**DATA BACKUP AT RESET**\n\n{backup_lines[:4000]}",
        color=config.FIRE_RED,
        timestamp=datetime.now(timezone.utc),
    )
    embed.set_footer(text=f"RESET EXECUTED BY {actor}")

    channel = await _fetch_log_channel(bot)
    if channel:
        try:
            await channel.send(embed=embed)
        except discord.HTTPException as e:
            logger.warning(f"Backup send failed: {e}")
    return embed
