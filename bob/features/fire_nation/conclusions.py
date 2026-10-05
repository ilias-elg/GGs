"""
Automatic merit counting from conclusion posts.

When someone posts a "... Concluded" message in a watched channel, Bob works
out who earned what and posts a card with Approve / Reject buttons in the owner
log channel. Nothing is recorded until someone with the rank to award that
merit type approves it.

The buttons carry the conclusion's channel and message ID, and approving
re-reads that message — so they keep working across restarts, and an edit made
before approval is what gets counted.
"""

import asyncio
import logging
import re
from dataclasses import dataclass

import discord

import config

from . import merit
from .ranks import get_rank

logger = logging.getLogger("discord")

_MENTION = re.compile(r"<@!?(\d+)>")
_COHOST_LINE = re.compile(r"co[\s-]?host", re.IGNORECASE)
_TITLE_NOISE = re.compile(r"<a?:\w+:\d+>|[#*_`『』]")
# The title's wording varies with what was hosted ("Soldier Exam Concluded",
# "Recruit Induction Concluded"), so the type comes from a keyword in it.
# (keyword, merit type whose rules apply, name shown on the card, the labelled
# lines whose pings earn the merit — None means every ping in the post)
_COHOST = r"co[\s-]?host"
_EXAM_LABELS = rf"{_COHOST}|guards?|spectators?"
_TRAINING_LABELS = rf"{_COHOST}|attendees?|spectators?"
# For /addmerit, which reads the same lines: activity → (labels, how to name them in a reply).
COUNTED_LINES = {
    "exam": (_EXAM_LABELS, "**Co-host**, **Guards** and **Spectators**"),
    "training": (_TRAINING_LABELS, "**Co-host**, **Attendees** and **Spectators**"),
}
_TYPE_KEYWORDS = (
    ("raid", "raid", "Raid", None),
    # Merits are for running or watching an exam, not for passing it: the
    # people on the "Passed:" line get nothing.
    ("exam", "exam", "Exam", _EXAM_LABELS),
    ("induction", "exam", "Exam", _EXAM_LABELS),
    # A training is worth the same as an event. Its post also pings winners
    # and teams, which don't count.
    ("training", "event", "Training", _TRAINING_LABELS),
    ("event", "event", "Event", None),
)
# "Something:" anywhere in the text. The lookarounds keep timestamps (<t:1:R>),
# emoji (<:name:1>, :name:) and links (https://) from counting as labels.
# A bold or underlined heading counts as a label with or without its colon
# ("**Spectators**", "__FFA__").
_LABEL = re.compile(
    r"(?P<mark>\*\*|__)\s*(?P<bold>[A-Za-z][A-Za-z -]{0,30}?)\s*:?\s*(?P=mark):?"
    r"|(?<![\w<:@/*])(?P<plain>[A-Za-z][A-Za-z -]{0,30}):(?!\d|//)"
)

# Two people pressing Approve at once must not both get past the duplicate check.
_approval_lock = asyncio.Lock()


@dataclass
class Plan:
    merit_type: str
    label: str
    title: str
    host: discord.Member
    cohost: discord.Member | None
    awards: list[tuple[discord.Member, float]]
    skipped: int


def read_title(content: str) -> tuple[str, str, str | None, str] | None:
    """(merit type, card label, counted labels, cleaned title) when the post opens with a recognised "... Concluded" line."""
    for line in [l for l in content.splitlines() if l.strip()][:3]:
        lowered = line.lower()
        if "concluded" not in lowered:
            continue
        for keyword, merit_type, label, counted in _TYPE_KEYWORDS:
            if keyword in lowered:
                return merit_type, label, counted, " ".join(_TITLE_NOISE.sub("", line).split())
        return None
    return None


def counted_text(content: str, labels: str) -> str:
    """
    Only the sections of a post whose label matches (e.g. "Co-host:" and
    "Guards:"), one per line with its label. A section runs from its label to
    the next "Something:" label or blank line, so a long list can wrap over
    several lines — and it still works on text pasted into a slash command,
    where the whole post arrives as a single line.
    """
    counted = re.compile(rf"\b(?:{labels})\b", re.IGNORECASE)
    found = list(_LABEL.finditer(content))
    kept = []
    for index, label in enumerate(found):
        name = label["bold"] or label["plain"]
        if not counted.search(name):
            continue
        end = found[index + 1].start() if index + 1 < len(found) else len(content)
        section = re.split(r"\n\s*\n", content[label.end():end], maxsplit=1)[0]
        kept.append(f"{name}: {' '.join(section.split())}")
    return "\n".join(kept)


def counted_text_for(activity: str, content: str) -> str:
    """The part of an exam or training post whose pings earn the merit — for /addmerit."""
    return counted_text(content, COUNTED_LINES[activity][0])


async def _member(guild: discord.Guild, user_id: int) -> discord.Member | None:
    member = guild.get_member(user_id)
    if member:
        return member
    try:
        return await guild.fetch_member(user_id)
    except discord.HTTPException:
        return None


async def build_plan(message: discord.Message) -> Plan | None:
    """Who gets what for this conclusion post, or None when it isn't one."""
    if message.guild is None or message.author.bot or not isinstance(message.author, discord.Member):
        return None
    title = read_title(message.content)
    if not title:
        return None
    merit_type, label, counted, title_text = title
    host = message.author
    content = counted_text(message.content, counted) if counted else message.content

    mention_ids = list(dict.fromkeys(int(i) for i in _MENTION.findall(content)))
    found = [await _member(message.guild, user_id) for user_id in mention_ids]
    recipients = [m for m in found if m and not m.bot]
    # The host always receives the merit, pinged in their own post or not.
    if all(m.id != host.id for m in recipients):
        recipients.append(host)

    cohost = None
    if merit_type in merit.COHOST_MERIT_TYPES:
        for line in content.splitlines():
            # The co-host is the first person pinged after the word itself, so
            # "thanks @a @b and co-host @c" picks c, not a.
            label = _COHOST_LINE.search(line)
            ids = _MENTION.findall(line[label.end():]) if label else []
            if ids:
                cohost = next((m for m in recipients if m.id == int(ids[0])), None)
                break
        if cohost and cohost.id == host.id:
            cohost = None

    amount = merit.fixed_merit_amount(merit_type)
    awards = [(m, amount) for m in recipients]
    if cohost:
        awards.append((cohost, merit.COHOST_BONUS_AMOUNT))
    return Plan(merit_type, label, title_text, host, cohost, awards, len(mention_ids) - len([m for m in found if m]))


def _card(plan: Plan, message: discord.Message, heading: str, color: int) -> discord.Embed:
    totals: dict[int, float] = {}
    members: dict[int, discord.Member] = {}
    for member, amount in plan.awards:
        totals[member.id] = totals.get(member.id, 0) + amount
        members[member.id] = member
    host_id, cohost_id = plan.host.id, getattr(plan.cohost, "id", None)
    order = sorted(totals, key=lambda i: (i != host_id, i != cohost_id, str(members[i]).lower()))
    lines = [
        f"`+{merit.fmt_amount(totals[i]):<3}` {merit._who(members[i])}"
        + (" · **host**" if i == host_id else " · **co-host**" if i == cohost_id else "")
        for i in order
    ]
    grand_total = sum(totals.values())
    embed = merit._audit_embed(
        heading,
        f"**{plan.title}**\n**{len(totals)}** member{'' if len(totals) == 1 else 's'} · "
        f"**{merit.fmt_amount(grand_total)}** merit{merit.plural(grand_total)} in total",
        color,
    )
    for index, chunk in enumerate(merit._chunk_lines(lines)[:20]):
        embed.add_field(name="Recipients" if index == 0 else "​", value=chunk, inline=False)
    if plan.skipped:
        embed.add_field(
            name="Skipped", value=f"{plan.skipped} pinged user(s) are no longer in the server.", inline=False
        )
    embed.add_field(name="Proof", value=f"[Open the message]({message.jump_url})", inline=True)
    return embed


class ConclusionButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"conclusion:(?P<action>approve|reject):(?P<channel>\d+):(?P<message>\d+)",
):
    def __init__(self, action: str, channel_id: int, message_id: int) -> None:
        super().__init__(
            discord.ui.Button(
                label="Approve" if action == "approve" else "Reject",
                style=discord.ButtonStyle.success if action == "approve" else discord.ButtonStyle.danger,
                custom_id=f"conclusion:{action}:{channel_id}:{message_id}",
            )
        )
        self.action, self.channel_id, self.message_id = action, channel_id, message_id

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match: re.Match):
        return cls(match["action"], int(match["channel"]), int(match["message"]))

    async def _close(self, interaction: discord.Interaction, note: str, embed: discord.Embed | None = None) -> None:
        """Ends the card: no more buttons, and a line saying how it ended."""
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
        await interaction.response.defer()

        channel = interaction.client.get_channel(self.channel_id)
        try:
            message = await channel.fetch_message(self.message_id) if channel else None
        except discord.HTTPException:
            message = None
        plan = await build_plan(message) if message else None
        if plan is None:
            await self._close(
                interaction, "The conclusion post was deleted or is no longer a conclusion — nothing recorded."
            )
            return

        try:
            merit.assert_can_award(actor_rank, plan.merit_type)
            if self.action == "reject":
                await self._close(interaction, f"Rejected by {merit._who(actor)} — nothing recorded.")
                return
            for member, _ in plan.awards:
                merit.assert_not_protected_owner(actor_rank, member.id)
            async with _approval_lock:
                if await merit.proof_recorded(message.jump_url):
                    await self._close(interaction, "Already recorded — nothing was added twice.")
                    return
                await merit.record_awards(message.guild.id, plan.awards, message.jump_url, actor)
        except merit.MeritError as e:
            await interaction.followup.send(f"Could not do that: {e}", ephemeral=True)
            return

        await self._close(
            interaction,
            f"Approved by {merit._who(actor)}",
            _card(plan, message, f"{plan.label} merits recorded", config.FIRE_ORANGE),
        )
        try:
            await message.add_reaction("✅")
        except discord.HTTPException:
            pass


async def handle_message(bot: discord.Client, message: discord.Message) -> None:
    """Posts an approval card for a conclusion written in a watched channel."""
    if message.channel.id not in config.MERIT_CONCLUSION_CHANNEL_IDS:
        return
    plan = await build_plan(message)
    if plan is None:
        return
    channel = await merit._fetch_log_channel(bot)
    if channel is None:
        return
    view = discord.ui.View(timeout=None)
    view.add_item(ConclusionButton("approve", message.channel.id, message.id))
    view.add_item(ConclusionButton("reject", message.channel.id, message.id))
    try:
        await channel.send(
            embed=_card(plan, message, f"{plan.label} conclusion — awaiting approval", config.FIRE_RED), view=view
        )
    except discord.HTTPException as e:
        logger.error(f"Conclusion approval card send failed: {e}")
