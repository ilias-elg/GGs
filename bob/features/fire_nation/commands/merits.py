"""Merit commands: awarding, removing, looking up and resetting merits."""

import logging
import re
from datetime import datetime, time, timezone

import discord
from discord import app_commands
from discord.ext import commands, tasks

import config

from .. import conclusions, merit
from ..ranks import can_manage, get_rank
from .base import FireNationCog
from .paging import send_paged

logger = logging.getLogger("discord")

_MENTION = re.compile(r"<@!?(\d+)>")
_MESSAGE_LINK = re.compile(
    r"^https://(?:(?:canary|ptb)\.)?discord(?:app)?\.com/channels/(?:\d+|@me)/\d+/\d+/?(?:\?.*)?$"
)


def extract_mention_ids(text: str) -> list[int]:
    """Every unique user ID from a blob containing <@ID> or <@!ID> mentions."""
    return list(dict.fromkeys(int(m) for m in _MENTION.findall(text)))


def is_discord_message_link(url: str) -> bool:
    """True only for a real Discord message link (channels/<guild>/<channel>/<message>)."""
    return bool(_MESSAGE_LINK.match(url.strip()))


async def _fetch_member(guild: discord.Guild, user_id: int) -> discord.Member | None:
    member = guild.get_member(user_id)
    if member:
        return member
    try:
        return await guild.fetch_member(user_id)
    except discord.HTTPException:
        return None


async def _fetch_members(guild: discord.Guild, user_ids: list[int]) -> list[discord.Member]:
    """Resolves IDs to members, silently skipping anyone who left the server."""
    members = [await _fetch_member(guild, user_id) for user_id in user_ids]
    return [m for m in members if m]


def _skipped_note(skipped: int) -> str:
    if skipped <= 0:
        return ""
    return f" ({skipped} mention{'' if skipped == 1 else 's'} not found in server — skipped)"


LEADERBOARD_PAGE_SIZE = 15
MERIT_HISTORY_PAGE_SIZE = 10


def build_leaderboard_embed(rows: list[dict], page: int, total_pages: int, all_time: bool = False) -> discord.Embed:
    start = page * LEADERBOARD_PAGE_SIZE
    lines = [
        f"**{start + i + 1:02d}**  {row['member_tag'][:45]}  —  **{merit.fmt_amount(row['total'])}**"
        for i, row in enumerate(rows[start:start + LEADERBOARD_PAGE_SIZE])
    ]
    embed = discord.Embed(
        title="FIRE NATION // MERIT COMMAND",
        description=(
            ("**ALL-TIME PERSONNEL RANKING**" if all_time else "**CURRENT PERSONNEL RANKING**")
            + "\n\n" + "\n".join(lines)
        ),
        color=config.FIRE_RED,
        timestamp=datetime.now(timezone.utc),
    )
    embed.set_footer(
        text=f"FIRE NATION • MERIT SYSTEM • Page {page + 1}/{total_pages} • {len(rows)} total • AUTHORIZED PERSONNEL ONLY"
    )
    return embed


def build_history_embed(target_tag: str, rows: list[dict], page: int, total_pages: int) -> discord.Embed:
    start = page * MERIT_HISTORY_PAGE_SIZE
    lines = []
    for row in rows[start:start + MERIT_HISTORY_PAGE_SIZE]:
        proof = row["proof_url"].strip()
        if re.match(r"https?://", proof, re.IGNORECASE):
            proof = f"[Proof of action]({proof})"
        sign = "+" if row["amount"] > 0 else ""
        lines.append(f"**{sign}{merit.fmt_amount(row['amount'])}**  •  {proof}  •  <t:{row['created_at']}:R>")
    embed = discord.Embed(
        title="FIRE NATION // MERIT HISTORY",
        description=f"**PERSONNEL:** {target_tag}\n\n" + "\n".join(lines),
        color=config.FIRE_ORANGE,
        timestamp=datetime.now(timezone.utc),
    )
    embed.set_footer(
        text=f"FIRE NATION • VERIFIED ACTION HISTORY • Page {page + 1}/{total_pages} • {len(rows)} total"
    )
    return embed


class ResetConfirmView(discord.ui.View):
    def __init__(self, bot: commands.Bot, interaction: discord.Interaction) -> None:
        super().__init__(timeout=30)
        self.bot = bot
        self.interaction = interaction
        self.done = False

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.interaction.user.id

    @discord.ui.button(label="Yes, Reset the Current Leaderboard", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.done = True
        self.stop()
        await interaction.response.defer()
        try:
            merit.assert_can_manage_data(get_rank(interaction.user))
            entries = await merit.reset_all_data(interaction.guild.id)
            backup = await merit.audit_reset(self.bot, entries, interaction.user)
            await interaction.edit_original_response(
                content="✅ **THE CURRENT LEADERBOARD HAS BEEN RESET.** The all-time leaderboard is unchanged.",
                embed=backup, view=None,
            )
        except Exception as e:
            logger.error(f"Error during merit data reset: {e}", exc_info=True)
            await interaction.edit_original_response(
                content="❌ An error occurred during the data reset.", view=None
            )

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.done = True
        self.stop()
        await interaction.response.edit_message(content="❌ Data reset cancelled.", view=None)

    async def on_timeout(self) -> None:
        if self.done:
            return
        try:
            await self.interaction.edit_original_response(
                content="⏱️ Confirmation timed out. Data reset cancelled.", view=None
            )
        except discord.HTTPException:
            pass


class MeritCommands(FireNationCog):
    """/addmerit, /removemerit, /merits, /leaderboard, /merithistory, /resetdata, plus conclusion cards."""

    async def cog_load(self) -> None:
        self.prune_departed.start()
        self.bot.add_dynamic_items(conclusions.ConclusionButton)

    async def cog_unload(self) -> None:
        self.prune_departed.cancel()

    @tasks.loop(time=time(0, 0, tzinfo=timezone.utc))
    async def prune_departed(self) -> None:
        """Midnight UTC: take everyone who left the home server off the leaderboard."""
        try:
            departed = await merit.prune_departed_members(self.bot)
            if departed:
                await merit.audit_prune(self.bot, departed)
        except Exception as e:
            logger.error(f"Daily merit cleanup failed: {e}", exc_info=True)

    @prune_departed.before_loop
    async def _before_prune(self) -> None:
        await self.bot.wait_until_ready()

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        # Includes bot/webhook posts — announcements followed from another
        # server arrive as webhook messages.
        await conclusions.handle_message(self.bot, message)

    # ── /addmerit ────────────────────────────────────────────────────────────

    addmerit = app_commands.Group(
        name="addmerit", description="Award merits based on activity type.", guild_only=True
    )

    async def _award_activity(
        self,
        interaction: discord.Interaction,
        merit_type: str,
        announcement: str,
        proof: str,
        host: discord.Member,
        cohost: discord.Member | None,
        activity: str | None = None,
        spectators: str | None = None,
    ) -> None:
        """
        exam / event / raid — the host plus everyone pinged in the announcement;
        co-hosts (exam/event) get +0.5. Pings on the announcement's Spectators
        line, or in `spectators`, get merit.SPECTATOR_AMOUNT instead.
        `activity` names what was actually run when it differs from the merit
        type: a training is recorded with the event rules.
        """
        activity = activity or merit_type
        await interaction.response.defer(ephemeral=True)
        guild, actor = interaction.guild, interaction.user
        try:
            actor_rank = get_rank(actor)
            merit.assert_can_award(actor_rank, merit_type)

            proof = proof.strip()
            if not is_discord_message_link(proof):
                raise merit.MeritError(
                    "Invalid proof URL. Please provide a valid Discord message link "
                    "(right-click the conclusion message → Copy Message Link). It looks like "
                    "`https://discord.com/channels/<guild_id>/<channel_id>/<message_id>`."
                )
            # Exams and trainings only count certain lines of the pasted post:
            # passing an exam or winning a training round earns nothing.
            _, mention_ids, spectator_ids = conclusions.split_counted(
                announcement, conclusions.labels_for(activity)
            )
            spectator_ids += [
                i for i in extract_mention_ids(spectators or "") if i not in mention_ids and i not in spectator_ids
            ]
            spectator_ids = [i for i in spectator_ids if i != host.id]
            if activity not in conclusions.COUNTED_LINES and not mention_ids and not spectator_ids:
                raise merit.MeritError(
                    "No @mentions found in the announcement. Make sure you pasted the full conclusion text."
                )
            for lead, label in ((host, "host"), (cohost, "co-host")):
                if lead is None:
                    continue
                if not isinstance(lead, discord.Member):
                    raise merit.MeritError(f"The specified {label} is not currently in the server.")
                merit.assert_not_protected_owner(actor_rank, lead.id)

            recipients = await _fetch_members(guild, mention_ids + spectator_ids)
            mentioned_count = len(recipients)
            for m in recipients:
                merit.assert_not_protected_owner(actor_rank, m.id)
            if cohost and cohost.id == host.id:
                cohost = None
            # The host always receives the merit, tagged in the announcement or not.
            if all(m.id != host.id for m in recipients):
                recipients.append(host)
            # Only members of the home server earn merits; anyone else is left out.
            in_home = [m for m in recipients if await merit.in_home_server(self.bot, m.id)]
            outside = len(recipients) - len(in_home)
            recipients = in_home
            if cohost and all(m.id != cohost.id for m in recipients) and not await merit.in_home_server(self.bot, cohost.id):
                cohost = None
                outside += 1
            if not recipients:
                raise merit.MeritError("Nobody named in this award is in the military server.")

            amount = merit.fixed_merit_amount(merit_type)
            label = activity.capitalize()
            watching = frozenset(spectator_ids)
            awards = [(m, merit.SPECTATOR_AMOUNT if m.id in watching else amount) for m in recipients]
            spectator_count = sum(1 for m in recipients if m.id in watching)
            # The co-host bonus is extra: it stacks with the participant merit
            # they get from being pinged in the announcement.
            if cohost:
                awards.append((cohost, merit.COHOST_BONUS_AMOUNT))
            await merit.record_awards(guild.id, awards, proof, actor)
            await merit.audit_award(
                self.bot, awards, label, actor, proof, host=host, cohost=cohost, spectators=watching
            )

            outside_note = (
                f"\n• **{outside}** left out — not in the military server." if outside else ""
            )
            exam_note = ""
            if activity in conclusions.COUNTED_LINES:
                exam_note = f"\n• Only the {conclusions.COUNTED_LINES[activity][1]} lines were counted."
                if not mention_ids and not spectator_ids:
                    exam_note += " No pings were found on those lines, so only the host was credited."
            cohost_note = ""
            if cohost:
                pinged = any(m.id == cohost.id for m in recipients)
                cohost_note = (
                    f"\n• **Co-host:** {cohost} — **+{merit.fmt_amount(merit.COHOST_BONUS_AMOUNT)}** bonus"
                    + ("" if pinged else " only (not pinged in the announcement, so no participant merit)")
                )
            participants = len(recipients) - spectator_count
            spectator_note = (
                f"\n• **{spectator_count}** spectator{'' if spectator_count == 1 else 's'} — "
                f"**+{merit.fmt_amount(merit.SPECTATOR_AMOUNT)}** each"
                if spectator_count else ""
            )
            await interaction.edit_original_response(content=(
                f"Recorded **+{merit.fmt_amount(amount)}** {label} merit{merit.plural(amount)} for "
                f"**{participants}** member{'' if participants == 1 else 's'} (Host: {host})"
                f"{_skipped_note(len(mention_ids) + len(spectator_ids) - mentioned_count)} — logged for owners."
                f"{spectator_note}{outside_note}{cohost_note}{exam_note}\n• **Proof:** <{proof}>"
            ))
        except merit.MeritError as e:
            logger.warning(f"Merit award rejected for {actor.id}: {e}")
            await interaction.edit_original_response(content=f"Could not record the award: {e}")

    @addmerit.command(name="exam", description="Award 1 merit to the host, co-host, guards and spectators. Paste the conclusion.")
    @app_commands.describe(
        announcement="Paste the full exam conclusion — Co-host, Guards and Spectators are counted, not Passed.",
        proof="Discord message link as proof",
        host="The host who ran this exam — receives the merit.",
        cohost="Optional co-host — gets an extra 0.5 on top of their participant merit.",
        spectators="Optional: @mention spectators not already on the post's Spectators line — 1 merit each.",
    )
    # Option names are deliberately not plain words like "host" or "proof":
    # Discord's command box treats "<option name>:" anywhere in pasted text as
    # the start of that option, so an announcement containing "co-host: @x"
    # used to get split across the fields.
    @app_commands.rename(host="hosted_by", cohost="cohosted_by", proof="proof_link", spectators="spectated_by")
    async def addmerit_exam(
        self, interaction: discord.Interaction, announcement: str, proof: str,
        host: discord.Member, cohost: discord.Member | None = None, spectators: str | None = None,
    ) -> None:
        await self._award_activity(interaction, "exam", announcement, proof, host, cohost, spectators=spectators)

    @addmerit.command(
        name="training", description="Award 1 merit to the host, co-host, attendees and spectators. Paste the conclusion."
    )
    @app_commands.describe(
        announcement="Paste the full training conclusion — Co-host, Attendees and Spectators are counted.",
        proof="Discord message link as proof",
        host="The host who ran this training — receives the merit.",
        cohost="Optional co-host — gets an extra 0.5 on top of their participant merit.",
        spectators="Optional: @mention spectators not already on the post's Spectators line — 1 merit each.",
    )
    @app_commands.rename(host="hosted_by", cohost="cohosted_by", proof="proof_link", spectators="spectated_by")
    async def addmerit_training(
        self, interaction: discord.Interaction, announcement: str, proof: str,
        host: discord.Member, cohost: discord.Member | None = None, spectators: str | None = None,
    ) -> None:
        await self._award_activity(
            interaction, "event", announcement, proof, host, cohost, activity="training", spectators=spectators
        )

    @addmerit.command(name="event", description="Award 1 merit to all participants and spectators. Paste the conclusion.")
    @app_commands.describe(
        announcement="Paste the full event conclusion — every @mention is extracted automatically.",
        proof="Discord message link as proof",
        host="The host who ran this event — receives the merit.",
        cohost="Optional co-host — gets an extra 0.5 on top of their participant merit.",
        spectators="Optional: @mention spectators not already on the post's Spectators line — 1 merit each.",
    )
    @app_commands.rename(host="hosted_by", cohost="cohosted_by", proof="proof_link", spectators="spectated_by")
    async def addmerit_event(
        self, interaction: discord.Interaction, announcement: str, proof: str,
        host: discord.Member, cohost: discord.Member | None = None, spectators: str | None = None,
    ) -> None:
        await self._award_activity(interaction, "event", announcement, proof, host, cohost, spectators=spectators)

    @addmerit.command(name="raid", description="Award 3 merits to participants, 1 to spectators. Advisor and above only.")
    @app_commands.describe(
        announcement="Paste the full raid conclusion — pings on the Spectators line get 1, everyone else 3.",
        proof="Discord message link as proof",
        host="The host who led this raid — receives the merit.",
        spectators="Optional: @mention spectators not already on the post's Spectators line — 1 merit each.",
    )
    @app_commands.rename(host="hosted_by", proof="proof_link", spectators="spectated_by")
    async def addmerit_raid(
        self, interaction: discord.Interaction, announcement: str, proof: str, host: discord.Member,
        spectators: str | None = None,
    ) -> None:
        await self._award_activity(interaction, "raid", announcement, proof, host, None, spectators=spectators)

    @addmerit.command(
        name="bonus", description="Award 0.1–50 bonus merits to one or more members. Advisor and above only."
    )
    @app_commands.describe(
        users="@mention one or more members to award, e.g. @Alice @Bob.",
        amount="Merit amount (0.1–50).",
        reason="Why this bonus is being awarded.",
    )
    async def addmerit_bonus(
        self, interaction: discord.Interaction, users: str, amount: app_commands.Range[float, 0.1, 50.0],
        reason: app_commands.Range[str, 1, 300],
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        guild, actor = interaction.guild, interaction.user
        try:
            actor_rank = get_rank(actor)
            merit.assert_can_award(actor_rank, "bonus")
            merit.assert_valid_amount(amount)

            mention_ids = extract_mention_ids(users)
            if not mention_ids:
                raise merit.MeritError("No @mentions found. Make sure you @mention one or more members.")
            for user_id in mention_ids:
                merit.assert_not_protected_owner(actor_rank, user_id)
            recipients = await _fetch_members(guild, mention_ids)
            if not recipients:
                raise merit.MeritError("None of the mentioned members were found in this server.")

            reason = " ".join(reason.split())
            if not reason:
                raise merit.MeritError("A reason is required for a bonus.")
            await merit.record_award(guild.id, recipients, amount, f"Bonus: {reason}", actor)
            await merit.audit_award(self.bot, [(m, amount) for m in recipients], "Bonus", actor, reason=reason)
            await interaction.edit_original_response(content=(
                f"Recorded **+{merit.fmt_amount(amount)}** Bonus merit{merit.plural(amount)} for "
                f"**{len(recipients)}** member{'' if len(recipients) == 1 else 's'}"
                f"{_skipped_note(len(mention_ids) - len(recipients))} — logged for owners."
                f"\n• **Reason:** {discord.utils.escape_markdown(reason)}"
            ))
        except merit.MeritError as e:
            logger.warning(f"Merit award rejected for {actor.id}: {e}")
            await interaction.edit_original_response(content=f"Could not record the award: {e}")

    # ── /removemerit ─────────────────────────────────────────────────────────

    @app_commands.command(name="removemerit", description="Remove merits from a member. Advisor and above only.")
    @app_commands.guild_only()
    @app_commands.describe(
        user="The member to deduct merits from.",
        amount="Merit amount to remove (0.1–50).",
        reason="Reason for the removal.",
    )
    async def removemerit(
        self, interaction: discord.Interaction, user: discord.Member,
        amount: app_commands.Range[float, 0.1, 50.0], reason: str,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        actor = interaction.user
        try:
            actor_rank = get_rank(actor)
            merit.assert_can_remove(actor_rank)
            merit.assert_valid_amount(amount)
            merit.assert_not_protected_owner(actor_rank, user.id)

            await merit.record_removal(interaction.guild.id, user, amount, reason, actor)
            await merit.audit_removal(self.bot, user, amount, reason, actor)
            await interaction.edit_original_response(content=(
                f"Recorded **-{merit.fmt_amount(amount)}** merit{merit.plural(amount)} for {user} — logged for owners."
            ))
        except merit.MeritError as e:
            logger.warning(f"Merit removal rejected for {actor.id}: {e}")
            await interaction.edit_original_response(content=f"Could not remove merits: {e}")

    # ── /merits, /leaderboard, /merithistory ─────────────────────────────────

    async def _send_leaderboard(self, interaction: discord.Interaction, all_time: bool = False) -> None:
        rows = await merit.get_leaderboard(all_time=all_time)
        if not rows:
            await interaction.edit_original_response(
                content="No merit data recorded yet." if all_time else "Nobody has earned merits since the last reset."
            )
            return
        await send_paged(
            interaction, len(rows), LEADERBOARD_PAGE_SIZE,
            lambda page, total: build_leaderboard_embed(rows, page, total, all_time),
        )

    @app_commands.command(name="merits", description="View a member's merit total or the full leaderboard.")
    @app_commands.guild_only()
    @app_commands.describe(user="The member to look up. Leave empty for the leaderboard.")
    async def merits(self, interaction: discord.Interaction, user: discord.User | None = None) -> None:
        await interaction.response.defer()
        if user is None:
            await self._send_leaderboard(interaction)
            return
        total = await merit.get_member_total(user.id)
        all_time = await merit.get_member_total(user.id, all_time=True)
        embed = discord.Embed(
            title="FIRE NATION // MERIT INQUIRY",
            description=(
                f"**PERSONNEL:** {user}\n**CURRENT MERITS:** **{merit.fmt_amount(total)}**"
                f"\n**ALL-TIME MERITS:** **{merit.fmt_amount(all_time)}**"
            ),
            color=config.FIRE_RED,
            timestamp=datetime.now(timezone.utc),
        )
        embed.set_footer(text="FIRE NATION • MERIT SYSTEM • VERIFIED DATA")
        await interaction.edit_original_response(embed=embed)

    @app_commands.command(name="leaderboard", description="View every member ranked by merits — current or all-time.")
    @app_commands.guild_only()
    @app_commands.describe(board="Current (since the last reset, the default) or All Time (never reset).")
    @app_commands.choices(board=[
        app_commands.Choice(name="Current", value="current"),
        app_commands.Choice(name="All Time", value="all_time"),
    ])
    async def leaderboard(self, interaction: discord.Interaction, board: str = "current") -> None:
        await interaction.response.defer()
        await self._send_leaderboard(interaction, all_time=board == "all_time")

    @app_commands.command(name="merithistory", description="View a member's recent merit awards.")
    @app_commands.guild_only()
    @app_commands.describe(user="The member whose history to view.")
    async def merithistory(self, interaction: discord.Interaction, user: discord.User | None = None) -> None:
        await interaction.response.defer(ephemeral=True)
        try:
            merit.assert_can_view_history(get_rank(interaction.user))
        except merit.MeritError as e:
            await interaction.edit_original_response(content=str(e))
            return

        target = user or interaction.user
        history = await merit.get_member_history(target.id)
        if not history:
            await interaction.edit_original_response(content=f"No merit history found for **{target}**.")
            return
        await send_paged(
            interaction, len(history), MERIT_HISTORY_PAGE_SIZE,
            lambda page, total: build_history_embed(str(target), history, page, total),
        )

    # ── /resetdata ───────────────────────────────────────────────────────────

    @app_commands.command(
        name="resetdata", description="Reset the current leaderboard. The all-time leaderboard is kept."
    )
    @app_commands.guild_only()
    async def resetdata(self, interaction: discord.Interaction) -> None:
        if not can_manage(interaction.user):
            await interaction.response.send_message(
                "Access Denied — only the Owner or Fire Lord can reset system data.", ephemeral=True
            )
            return
        await interaction.response.send_message(
            "⚠️ **ARE YOU SURE?** This sets everyone's **current** merits back to 0. The **all-time** "
            "leaderboard keeps everything, and a backup of the current one is posted first.",
            view=ResetConfirmView(self.bot, interaction),
            ephemeral=True,
        )
