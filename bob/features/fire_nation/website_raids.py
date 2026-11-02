"""
Raid merits from the Fire Nation website's raid results.

The website's feed (config.RAID_RESULTS_API_URL) lists every raid with each
player's Roblox username and side. For each new raid Fire fought in, the
Archivist posts a card in the owner log channel: every Fire player on Fire's
side who can be matched to a member of the home server gets the raid merit
once someone with the rank approves.

Roblox names are matched to Discord members by their nickname, display name
or username (any word in it, so "vShadeXII | BST" matches vShadeXII), or by a
link an officer set with /robloxlink when the names differ. Players who can't
be matched are listed on the card instead of being guessed.
"""

import asyncio
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime

import discord
import httpx

import config

from . import merit
from .ranks import get_rank

logger = logging.getLogger("discord")

FIRE = "Fire"
_WORD_SPLIT = re.compile(r"[\s|\[\](){}/\\,@•·]+")

# Two people pressing Approve at once must not both get past the duplicate check.
_approval_lock = asyncio.Lock()


# ─── The feed ─────────────────────────────────────────────────────────────────


async def fetch_feed() -> dict | None:
    """The website's raid feed, or None when it can't be read or is serving stale data."""
    if not config.RAID_RESULTS_API_URL:
        return None
    try:
        async with httpx.AsyncClient(timeout=15, headers={"User-Agent": "FireNationArchivist/1.0"}) as client:
            res = await client.get(config.RAID_RESULTS_API_URL)
        res.raise_for_status()
        data = res.json()
    except (httpx.HTTPError, ValueError) as e:
        logger.warning(f"Raid results feed unavailable: {e}")
        return None
    if not isinstance(data, dict) or not isinstance(data.get("raids"), list):
        logger.warning("Raid results feed returned something unexpected — skipped.")
        return None
    if data.get("stale"):
        # The site couldn't reach its own source and is showing old results.
        return None
    return data


def proof_url(raid_id: str) -> str:
    return f"{config.RAID_RESULTS_PAGE_URL}#raid={raid_id}"


def fire_side(raid: dict) -> str | None:
    """ "Attackers" or "Defenders" — whichever side Fire fought on — or None if Fire wasn't in it."""
    if FIRE in (raid.get("attackers") or []):
        return "Attackers"
    if FIRE in (raid.get("defenders") or []):
        return "Defenders"
    return None


def fire_names(feed: dict) -> set[str]:
    """Lower-cased Roblox names the website knows as Fire: its Fire leaderboard plus players listed as Fire."""
    names = {str(p.get("name", "")).lower() for p in feed.get("leaderboard") or []}
    names |= {str(p.get("name", "")).lower() for p in feed.get("players") or [] if p.get("nation") == FIRE}
    names.discard("")
    return names


def fire_players(raid: dict, feed: dict) -> list[str]:
    """Roblox names of the Fire players on Fire's side of this raid."""
    side = fire_side(raid)
    if side is None:
        return []
    on_side = [str(p["name"]) for p in raid.get("players") or [] if p.get("side") == side and p.get("name")]
    allied = len((raid.get("attackers") if side == "Attackers" else raid.get("defenders")) or []) > 1
    if not allied:
        return on_side
    # Fire fought alongside another nation: only Fire's own players earn it.
    known = fire_names(feed)
    return [name for name in on_side if name.lower() in known]


# ─── Roblox name → Discord member ────────────────────────────────────────────


async def get_links() -> dict[str, int]:
    pool = await merit.get_pool()
    async with pool.acquire() as db:
        rows = await db.fetch("SELECT roblox_name, member_id FROM roblox_links")
    return {r["roblox_name"]: int(r["member_id"]) for r in rows}


async def set_link(roblox_name: str, member_id: int, actor_id: int) -> None:
    pool = await merit.get_pool()
    async with pool.acquire() as db:
        await db.execute(
            "INSERT INTO roblox_links (roblox_name, member_id, linked_by_id) VALUES ($1, $2, $3) "
            "ON CONFLICT (roblox_name) DO UPDATE SET member_id = $2, linked_by_id = $3, linked_at = now()",
            roblox_name.lower(), str(member_id), str(actor_id),
        )


async def remove_link(roblox_name: str) -> bool:
    pool = await merit.get_pool()
    async with pool.acquire() as db:
        result = await db.execute("DELETE FROM roblox_links WHERE roblox_name = $1", roblox_name.lower())
    return result.endswith(" 1")


def _name_words(member: discord.Member) -> set[str]:
    words = set()
    for name in (member.nick, member.global_name, member.name):
        if name:
            words |= {w.lower() for w in _WORD_SPLIT.split(name) if w}
    return words


def match_members(guild: discord.Guild, names: list[str], links: dict[str, int]) -> tuple[dict[str, discord.Member], list[str]]:
    """
    ({roblox name: member}, [names that matched nobody, or more than one person]).
    A link set with /robloxlink always wins over a name match.
    """
    by_word: dict[str, list[discord.Member]] = {}
    for member in guild.members:
        if member.bot:
            continue
        for word in _name_words(member):
            by_word.setdefault(word, []).append(member)

    matched, unmatched = {}, []
    for name in names:
        linked = links.get(name.lower())
        member = guild.get_member(linked) if linked else None
        if member is None:
            candidates = by_word.get(name.lower(), [])
            member = candidates[0] if len(candidates) == 1 else None
        if member:
            matched[name] = member
        else:
            unmatched.append(name)
    return matched, unmatched


# ─── Cards ────────────────────────────────────────────────────────────────────


@dataclass
class RaidPlan:
    raid: dict
    title: str
    awards: list[tuple[discord.Member, float]] = field(default_factory=list)
    roblox_names: dict[int, str] = field(default_factory=dict)
    unmatched: list[str] = field(default_factory=list)


def _title(raid: dict) -> str:
    attackers = " + ".join(raid.get("attackers") or ["?"])
    defenders = " + ".join(raid.get("defenders") or ["?"])
    side = fire_side(raid)
    winner = raid.get("winner")
    if winner == "Tie":
        result = "Tie"
    else:
        result = "Fire won" if winner == side else "Fire lost"
    return f"{attackers} raided {defenders} — {result}"


async def build_plan(bot: discord.Client, raid: dict, feed: dict) -> RaidPlan | None:
    """Who gets the raid merit for this website raid, or None when Fire wasn't in it or the server isn't available."""
    names = fire_players(raid, feed)
    guild = bot.get_guild(config.MERIT_HOME_GUILD_ID)
    if not names or guild is None:
        return None
    if not guild.chunked:
        await guild.chunk()
    matched, unmatched = match_members(guild, names, await get_links())
    amount = merit.fixed_merit_amount("raid")
    plan = RaidPlan(raid, _title(raid), unmatched=unmatched)
    seen: set[int] = set()
    for name, member in matched.items():
        if member.id in seen:
            continue  # two Roblox accounts linked to one person still earn once
        seen.add(member.id)
        plan.awards.append((member, amount))
        plan.roblox_names[member.id] = name
    return plan


def card(plan: RaidPlan, heading: str, color: int) -> discord.Embed:
    raid = plan.raid
    total = sum(a for _, a in plan.awards)
    minutes = int(raid.get("durationSeconds") or 0) // 60
    embed = merit._audit_embed(
        heading,
        f"**{plan.title}**\n{raid.get('gamemode') or 'Raid'} · {minutes}m · "
        f"<t:{_timestamp(raid)}:f>\n**{len(plan.awards)}** member{'' if len(plan.awards) == 1 else 's'} · "
        f"**{merit.fmt_amount(total)}** merit{merit.plural(total)} in total",
        color,
    )
    lines = [
        f"`+{merit.fmt_amount(amount):<3}` {merit._who(member)} · {discord.utils.escape_markdown(plan.roblox_names[member.id])}"
        for member, amount in sorted(plan.awards, key=lambda a: str(a[0]).lower())
    ]
    for index, chunk in enumerate(merit._chunk_lines(lines)[:20]):
        embed.add_field(name="Recipients" if index == 0 else "​", value=chunk, inline=False)
    if plan.unmatched:
        embed.add_field(
            name=f"Not matched ({len(plan.unmatched)}) — no merit",
            value=discord.utils.escape_markdown(", ".join(plan.unmatched))[:1000]
            + "\nLink them with `/robloxlink set`, then approve: the card is re-read on approval.",
            inline=False,
        )
    embed.add_field(name="Proof", value=f"[Raid results]({proof_url(raid['id'])})", inline=True)
    return embed


def _timestamp(raid: dict) -> int:
    try:
        return int(datetime.fromisoformat(str(raid["createdAt"]).replace("Z", "+00:00")).timestamp())
    except (KeyError, ValueError):
        return 0


class WebsiteRaidButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"webraid:(?P<action>approve|reject):(?P<raid>[0-9a-fA-F-]{8,64})",
):
    def __init__(self, action: str, raid_id: str) -> None:
        super().__init__(
            discord.ui.Button(
                label="Approve" if action == "approve" else "Reject",
                style=discord.ButtonStyle.success if action == "approve" else discord.ButtonStyle.danger,
                custom_id=f"webraid:{action}:{raid_id}",
            )
        )
        self.action, self.raid_id = action, raid_id

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match: re.Match):
        return cls(match["action"], match["raid"])

    async def _close(self, interaction: discord.Interaction, note: str, embed: discord.Embed | None = None) -> None:
        embed = embed or (interaction.message.embeds[0] if interaction.message.embeds else None)
        if embed:
            embed.add_field(name="Outcome", value=note, inline=False)
            await interaction.edit_original_response(embed=embed, view=None)
        else:
            await interaction.edit_original_response(content=note, view=None)

    async def callback(self, interaction: discord.Interaction) -> None:
        actor = interaction.user
        if not isinstance(actor, discord.Member):
            await interaction.response.send_message("This only works inside the server.", ephemeral=True)
            return
        actor_rank = get_rank(actor)
        try:
            merit.assert_can_award(actor_rank, "raid")
        except merit.MeritError as e:
            await interaction.response.send_message(f"Could not do that: {e}", ephemeral=True)
            return
        await interaction.response.defer()
        if self.action == "reject":
            await self._close(interaction, f"Rejected by {merit._who(actor)} — nothing recorded.")
            return

        # Re-read the website, so a corrected result or a link added since
        # the card was posted is what gets recorded.
        feed = await fetch_feed()
        raid = next((r for r in (feed or {}).get("raids", []) if r.get("id") == self.raid_id), None)
        if feed is None:
            await interaction.followup.send("The raid results site can't be reached right now — try again shortly.", ephemeral=True)
            return
        if raid is None:
            await self._close(interaction, "This raid is no longer on the website — nothing recorded.")
            return
        plan = await build_plan(interaction.client, raid, feed)
        if plan is None or not plan.awards:
            await self._close(interaction, "Nobody in this raid could be matched to a member — nothing recorded.")
            return

        url = proof_url(self.raid_id)
        try:
            for member, _ in plan.awards:
                merit.assert_not_protected_owner(actor_rank, member.id)
            async with _approval_lock:
                if await merit.proof_recorded(url):
                    await self._close(interaction, "Already recorded — nothing was added twice.")
                    return
                await merit.record_awards(config.MERIT_HOME_GUILD_ID, plan.awards, url, actor)
        except merit.MeritError as e:
            await interaction.followup.send(f"Could not do that: {e}", ephemeral=True)
            return
        await self._close(
            interaction, f"Approved by {merit._who(actor)}", card(plan, "Raid merits recorded", config.FIRE_ORANGE)
        )


# ─── Polling ──────────────────────────────────────────────────────────────────


async def _seen_ids() -> set[str]:
    pool = await merit.get_pool()
    async with pool.acquire() as db:
        rows = await db.fetch("SELECT raid_id FROM website_raids_seen")
    return {r["raid_id"] for r in rows}


async def _mark_seen(raid_ids: list[str]) -> None:
    if not raid_ids:
        return
    pool = await merit.get_pool()
    async with pool.acquire() as db:
        await db.executemany(
            "INSERT INTO website_raids_seen (raid_id) VALUES ($1) ON CONFLICT DO NOTHING", [(i,) for i in raid_ids]
        )


async def check_for_new_raids(bot: discord.Client) -> int:
    """Posts a card for each raid not seen before. Returns how many cards were posted."""
    feed = await fetch_feed()
    if feed is None:
        return 0
    raids = [r for r in feed["raids"] if isinstance(r, dict) and r.get("id")]
    seen = await _seen_ids()
    if not seen:
        # First run: everything already on the site predates this feature and
        # was handled by hand, so none of it is offered again.
        await _mark_seen([r["id"] for r in raids])
        logger.info(f"Raid results feed connected — {len(raids)} existing raid(s) marked as already handled.")
        return 0

    channel = await merit._fetch_log_channel(bot)
    posted = 0
    # Oldest first, so the cards arrive in the order the raids happened.
    for raid in sorted((r for r in raids if r["id"] not in seen), key=lambda r: str(r.get("createdAt", ""))):
        plan = await build_plan(bot, raid, feed) if fire_side(raid) else None
        if plan is not None and channel is not None and (plan.awards or plan.unmatched):
            view = discord.ui.View(timeout=None)
            view.add_item(WebsiteRaidButton("approve", raid["id"]))
            view.add_item(WebsiteRaidButton("reject", raid["id"]))
            try:
                await channel.send(embed=card(plan, "Website raid — awaiting approval", config.FIRE_RED), view=view)
                posted += 1
            except discord.HTTPException as e:
                logger.error(f"Website raid card send failed: {e}")
                continue  # not marked seen, so it is tried again next time
        elif fire_side(raid) and (channel is None or bot.get_guild(config.MERIT_HOME_GUILD_ID) is None):
            continue  # can't post or match yet; try again next time
        await _mark_seen([raid["id"]])
    return posted
