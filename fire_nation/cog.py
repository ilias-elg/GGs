"""
Slash commands and gateway listeners for the Fire Nation features: merits,
rank roles, the knowledge base, diagnostics and voice.

Every rule lives in the feature modules (merit.py, ranks.py, voice.py); the
handlers here only parse options, call those, and format the reply.
"""

import io
import logging
import re
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands

import config
from voice.manager import get_voice_manager

from . import diagnostics, greetings, knowledge, merit, presence, tts
from . import voice as voice_feature
from .ranks import can_manage, get_rank, has_access, rank_at_least

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


# ─── Paging ───────────────────────────────────────────────────────────────────

LEADERBOARD_PAGE_SIZE = 15
MERIT_HISTORY_PAGE_SIZE = 10


def build_leaderboard_embed(rows: list[dict], page: int, total_pages: int) -> discord.Embed:
    start = page * LEADERBOARD_PAGE_SIZE
    lines = [
        f"**{start + i + 1:02d}**  {row['member_tag'][:45]}  —  **{merit.fmt_amount(row['total'])}**"
        for i, row in enumerate(rows[start:start + LEADERBOARD_PAGE_SIZE])
    ]
    embed = discord.Embed(
        title="FIRE NATION // MERIT COMMAND",
        description="**FULL PERSONNEL RANKING**\n\n" + "\n".join(lines),
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


class PagedView(discord.ui.View):
    """Previous/Next buttons for a paged embed. Only the person who ran the command can page."""

    def __init__(self, interaction: discord.Interaction, total_pages: int, build_embed) -> None:
        super().__init__(timeout=5 * 60)
        self.interaction = interaction
        self.total_pages = total_pages
        self.build_embed = build_embed
        self.page = 0
        self._sync_buttons()

    def _sync_buttons(self) -> None:
        self.previous.disabled = self.page <= 0
        self.next.disabled = self.page >= self.total_pages - 1

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.interaction.user.id:
            return True
        await interaction.response.send_message(
            "Only the user who ran this command can control these pages. Run it yourself to browse.",
            ephemeral=True,
        )
        return False

    async def _show(self, interaction: discord.Interaction) -> None:
        self._sync_buttons()
        await interaction.response.edit_message(embed=self.build_embed(self.page, self.total_pages), view=self)

    @discord.ui.button(label="◀ Previous", style=discord.ButtonStyle.secondary)
    async def previous(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.page = max(0, self.page - 1)
        await self._show(interaction)

    @discord.ui.button(label="Next ▶", style=discord.ButtonStyle.secondary)
    async def next(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.page = min(self.total_pages - 1, self.page + 1)
        await self._show(interaction)

    async def on_timeout(self) -> None:
        try:
            await self.interaction.edit_original_response(view=None)
        except discord.HTTPException:
            pass


async def send_paged(interaction: discord.Interaction, row_count: int, page_size: int, build_embed) -> None:
    total_pages = max(1, -(-row_count // page_size))
    if total_pages <= 1:
        await interaction.edit_original_response(embed=build_embed(0, 1))
        return
    view = PagedView(interaction, total_pages, build_embed)
    await interaction.edit_original_response(embed=build_embed(0, total_pages), view=view)


class ResetConfirmView(discord.ui.View):
    def __init__(self, bot: commands.Bot, interaction: discord.Interaction) -> None:
        super().__init__(timeout=30)
        self.bot = bot
        self.interaction = interaction
        self.done = False

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.interaction.user.id

    @discord.ui.button(label="Yes, Reset Everything", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.done = True
        self.stop()
        await interaction.response.defer()
        try:
            merit.assert_can_manage_data(get_rank(interaction.user))
            entries = await merit.reset_all_data(interaction.guild.id)
            backup = await merit.audit_reset(self.bot, entries, interaction.user)
            await interaction.edit_original_response(
                content="✅ **ALL MERIT DATA HAS BEEN RESET.**", embed=backup, view=None
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


# ─── The cog ──────────────────────────────────────────────────────────────────


class FireNationCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.voice_manager = get_voice_manager(bot)

    async def cog_app_command_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ) -> None:
        logger.error(f"/{interaction.command.qualified_name if interaction.command else '?'} failed: {error}", exc_info=error)
        reply = f"Something went wrong running that command: {getattr(error, 'original', error)}"
        try:
            if interaction.response.is_done():
                await interaction.edit_original_response(content=reply, view=None)
            else:
                await interaction.response.send_message(reply, ephemeral=True)
        except discord.HTTPException:
            pass

    # ── Listeners ────────────────────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        await presence.start(self.bot)

    @commands.Cog.listener()
    async def on_voice_state_update(
        self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState
    ) -> None:
        await voice_feature.handle_voice_state_update(self.voice_manager, member, before, after)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        # Includes bot/webhook posts — announcements followed from another
        # server arrive as webhook messages.
        await voice_feature.handle_announcement_message(self.voice_manager, message)

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
    ) -> None:
        """exam / event / raid — every @mention in the announcement plus the host; co-hosts (exam/event) get +0.5."""
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
            mention_ids = extract_mention_ids(announcement)
            if not mention_ids:
                raise merit.MeritError(
                    "No @mentions found in the announcement. Make sure you pasted the full conclusion text."
                )
            for lead, label in ((host, "host"), (cohost, "co-host")):
                if lead is None:
                    continue
                if not isinstance(lead, discord.Member):
                    raise merit.MeritError(f"The specified {label} is not currently in the server.")
                merit.assert_not_protected_owner(actor_rank, lead.id)

            recipients = await _fetch_members(guild, mention_ids)
            mentioned_count = len(recipients)
            for m in recipients:
                merit.assert_not_protected_owner(actor_rank, m.id)
            if cohost and cohost.id == host.id:
                cohost = None
            # The host always receives the merit, tagged in the announcement or not.
            if all(m.id != host.id for m in recipients):
                recipients.append(host)

            amount = merit.fixed_merit_amount(merit_type)
            label = merit_type.capitalize()
            awards = [(m, amount) for m in recipients]
            # The co-host bonus is extra: it stacks with the participant merit
            # they get from being pinged in the announcement.
            if cohost:
                awards.append((cohost, merit.COHOST_BONUS_AMOUNT))
            await merit.record_awards(guild.id, awards, proof, actor)
            await merit.audit_award(self.bot, recipients, amount, label, actor, proof)
            if cohost:
                await merit.audit_award(
                    self.bot, [cohost], merit.COHOST_BONUS_AMOUNT, f"{label} (co-host bonus)", actor, proof
                )

            cohost_note = ""
            if cohost:
                pinged = any(m.id == cohost.id for m in recipients)
                cohost_note = (
                    f"\n• **Co-host:** {cohost} — **+{merit.fmt_amount(merit.COHOST_BONUS_AMOUNT)}** bonus"
                    + ("" if pinged else " only (not pinged in the announcement, so no participant merit)")
                )
            await interaction.edit_original_response(content=(
                f"Recorded **+{merit.fmt_amount(amount)}** {label} merit{merit.plural(amount)} for "
                f"**{len(recipients)}** member{'' if len(recipients) == 1 else 's'} (Host: {host})"
                f"{_skipped_note(len(mention_ids) - mentioned_count)} — logged for owners."
                f"{cohost_note}\n• **Proof:** <{proof}>"
            ))
        except merit.MeritError as e:
            logger.warning(f"Merit award rejected for {actor.id}: {e}")
            await interaction.edit_original_response(content=f"Could not record the award: {e}")

    @addmerit.command(name="exam", description="Award 1 merit to all participants. Paste the conclusion announcement.")
    @app_commands.describe(
        announcement="Paste the full exam conclusion — every @mention is extracted automatically.",
        proof="Discord message link as proof",
        host="The host who ran this exam — receives the merit.",
        cohost="Optional co-host — gets an extra 0.5 on top of their participant merit.",
    )
    # Option names are deliberately not plain words like "host" or "proof":
    # Discord's command box treats "<option name>:" anywhere in pasted text as
    # the start of that option, so an announcement containing "co-host: @x"
    # used to get split across the fields.
    @app_commands.rename(host="hosted_by", cohost="cohosted_by", proof="proof_link")
    async def addmerit_exam(
        self, interaction: discord.Interaction, announcement: str, proof: str,
        host: discord.Member, cohost: discord.Member | None = None,
    ) -> None:
        await self._award_activity(interaction, "exam", announcement, proof, host, cohost)

    @addmerit.command(name="event", description="Award 1 merit to all participants. Paste the conclusion announcement.")
    @app_commands.describe(
        announcement="Paste the full event conclusion — every @mention is extracted automatically.",
        proof="Discord message link as proof",
        host="The host who ran this event — receives the merit.",
        cohost="Optional co-host — gets an extra 0.5 on top of their participant merit.",
    )
    @app_commands.rename(host="hosted_by", cohost="cohosted_by", proof="proof_link")
    async def addmerit_event(
        self, interaction: discord.Interaction, announcement: str, proof: str,
        host: discord.Member, cohost: discord.Member | None = None,
    ) -> None:
        await self._award_activity(interaction, "event", announcement, proof, host, cohost)

    @addmerit.command(name="raid", description="Award 3 merits to all participants. Advisor and above only.")
    @app_commands.describe(
        announcement="Paste the full raid conclusion — every @mention is extracted automatically.",
        proof="Discord message link as proof",
        host="The host who led this raid — receives the merit.",
    )
    @app_commands.rename(host="hosted_by", proof="proof_link")
    async def addmerit_raid(
        self, interaction: discord.Interaction, announcement: str, proof: str, host: discord.Member,
    ) -> None:
        await self._award_activity(interaction, "raid", announcement, proof, host, None)

    @addmerit.command(
        name="bonus", description="Award 0.1–50 bonus merits to one or more members. Advisor and above only."
    )
    @app_commands.describe(
        users="@mention one or more members to award, e.g. @Alice @Bob.",
        amount="Merit amount (0.1–50).",
    )
    async def addmerit_bonus(
        self, interaction: discord.Interaction, users: str, amount: app_commands.Range[float, 0.1, 50.0]
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

            await merit.record_award(guild.id, recipients, amount, f"Bonus award authorized by {actor}", actor)
            await merit.audit_award(self.bot, recipients, amount, "Bonus", actor)
            await interaction.edit_original_response(content=(
                f"Recorded **+{merit.fmt_amount(amount)}** Bonus merit{merit.plural(amount)} for "
                f"**{len(recipients)}** member{'' if len(recipients) == 1 else 's'}"
                f"{_skipped_note(len(mention_ids) - len(recipients))} — logged for owners."
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

    async def _send_leaderboard(self, interaction: discord.Interaction) -> None:
        rows = await merit.get_leaderboard()
        if not rows:
            await interaction.edit_original_response(content="No merit data recorded yet.")
            return
        await send_paged(
            interaction, len(rows), LEADERBOARD_PAGE_SIZE,
            lambda page, total: build_leaderboard_embed(rows, page, total),
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
        embed = discord.Embed(
            title="FIRE NATION // MERIT INQUIRY",
            description=f"**PERSONNEL:** {user}\n**RECORDED MERIT TOTAL:** **{merit.fmt_amount(total)}**",
            color=config.FIRE_RED,
            timestamp=datetime.now(timezone.utc),
        )
        embed.set_footer(text="FIRE NATION • MERIT SYSTEM • VERIFIED DATA")
        await interaction.edit_original_response(embed=embed)

    @app_commands.command(name="leaderboard", description="View every member ranked by merit total.")
    @app_commands.guild_only()
    async def leaderboard(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        await self._send_leaderboard(interaction)

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

    @app_commands.command(name="resetdata", description="Wipe all merit data. Exports a backup before resetting.")
    @app_commands.guild_only()
    async def resetdata(self, interaction: discord.Interaction) -> None:
        if not can_manage(interaction.user):
            await interaction.response.send_message(
                "Access Denied — only the Owner or Fire Lord can reset system data.", ephemeral=True
            )
            return
        await interaction.response.send_message(
            "⚠️ **ARE YOU SURE?** This permanently wipes all merit data. A full backup will be generated first.",
            view=ResetConfirmView(self.bot, interaction),
            ephemeral=True,
        )

    # ── /createhr, /createadvisor, /createroyalty ────────────────────────────

    async def _create_rank_role(
        self, interaction: discord.Interaction, role_name: str, allowed: bool, denial: str, assign_to: str
    ) -> None:
        if not allowed:
            await interaction.response.send_message(denial, ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        existing = discord.utils.get(interaction.guild.roles, name=role_name)
        if existing:
            await interaction.edit_original_response(
                content=f"The {role_name} role already exists: {existing.mention}. Bob will recognize it."
            )
            return
        try:
            role = await interaction.guild.create_role(
                name=role_name,
                permissions=discord.Permissions.none(),
                reason=f"{role_name} rank created by an authorized administrator",
            )
        except discord.HTTPException as e:
            logger.warning(f"{role_name} role creation failed for {interaction.user.id}: {e}")
            await interaction.edit_original_response(content=f"Could not create the {role_name} role: {e}")
            return
        await interaction.edit_original_response(
            content=f"Created {role.mention} with no elevated Discord permissions. Assign it to {assign_to}."
        )

    @app_commands.command(name="createhr", description="Create the HR rank role with no elevated Discord permissions.")
    @app_commands.guild_only()
    async def createhr(self, interaction: discord.Interaction) -> None:
        await self._create_rank_role(
            interaction, config.HR_ROLE_NAME, rank_at_least(interaction.user, "royalty"),
            "Access Denied — Royalty and above only.", "HR members",
        )

    @app_commands.command(
        name="createadvisor",
        description="Create the Advisor rank role (above HR) with no elevated Discord permissions.",
    )
    @app_commands.guild_only()
    async def createadvisor(self, interaction: discord.Interaction) -> None:
        await self._create_rank_role(
            interaction, config.ADVISOR_ROLE_NAME, rank_at_least(interaction.user, "royalty"),
            "Access Denied — Royalty and above only.", "Advisors",
        )

    @app_commands.command(
        name="createroyalty",
        description="Create the Royalty role (between Fire Lord and Advisor) with no permissions.",
    )
    @app_commands.guild_only()
    async def createroyalty(self, interaction: discord.Interaction) -> None:
        await self._create_rank_role(
            interaction, config.ROYALTY_ROLE_NAME, can_manage(interaction.user),
            "Only the Owner or Fire Lord can create the Royalty role.", "Royalty members",
        )

    # ── /addknowledge, /reloadknowledge ──────────────────────────────────────

    @app_commands.command(
        name="addknowledge", description="Append an entry to the Fire Nation knowledge base. HR and above only."
    )
    @app_commands.guild_only()
    @app_commands.describe(
        entry="The knowledge entry to add.",
        keywords="Comma-separated words that should bring this entry up (optional, auto-picked if empty).",
    )
    async def addknowledge(self, interaction: discord.Interaction, entry: str, keywords: str | None = None) -> None:
        if not rank_at_least(interaction.user, "hr"):
            await interaction.response.send_message("Access Denied — HR and above only.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        entry = entry.strip()
        if not entry:
            await interaction.edit_original_response(content="Could not add to the knowledge base: the entry is empty.")
            return
        try:
            size = knowledge.add_knowledge_entry(entry, keywords, str(interaction.user))
        except OSError as e:
            logger.warning(f"addknowledge failed for {interaction.user.id}: {e}")
            await interaction.edit_original_response(content=f"Could not add to the knowledge base: {e}")
            return
        await interaction.edit_original_response(
            content=f"Knowledge base updated. Entry added and reloaded ({size} characters total)."
        )

    @app_commands.command(
        name="reloadknowledge", description="Reload the Fire Nation knowledge file without restarting Bob."
    )
    @app_commands.guild_only()
    async def reloadknowledge(self, interaction: discord.Interaction) -> None:
        if not rank_at_least(interaction.user, "hr"):
            await interaction.response.send_message("Access Denied — HR and above only.", ephemeral=True)
            return
        before = len(knowledge.cached_knowledge)
        after = len(knowledge.load_knowledge())
        await interaction.response.send_message(
            f"Knowledge base reloaded. ({before} → {after} characters)"
            if after > 0
            else "Knowledge base reload failed — the file could not be read. Check the logs.",
            ephemeral=True,
        )

    # ── /diagnostics ─────────────────────────────────────────────────────────

    @app_commands.command(
        name="diagnostics",
        description="Check Bob's AI access, server permissions, voice and database. Owner/Fire Lord only.",
    )
    @app_commands.guild_only()
    @app_commands.describe(
        tts="Also generate a short test voice clip (uses a little TTS quota).",
        voice="Voice for the test clip (implies tts). Default: TTS_VOICE or Algenib.",
    )
    @app_commands.choices(voice=[app_commands.Choice(name=label, value=name) for name, label in tts.VOICES])
    async def diagnostics_command(
        self, interaction: discord.Interaction, tts: bool = False, voice: str | None = None
    ) -> None:
        if not can_manage(interaction.user):
            await interaction.response.send_message(
                "Access Denied — only the Owner or Fire Lord can run diagnostics.", ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True)
        # Picking a voice only makes sense with a clip to listen to.
        embeds, wav = await diagnostics.build_report(
            interaction.guild, interaction.channel, self.voice_manager, tts or voice is not None, voice
        )
        files = [discord.File(io.BytesIO(wav), filename="bob-voice-test.wav")] if wav else []
        # Discord allows 6000 characters of embed text per message, so a long
        # report continues in follow-ups instead of failing to send at all.
        await interaction.edit_original_response(embed=embeds[0], attachments=files)
        for extra in embeds[1:]:
            await interaction.followup.send(embed=extra, ephemeral=True)

    # ── /voice ───────────────────────────────────────────────────────────────

    voice = app_commands.Group(
        name="voice",
        description="Bob's voice: join your voice channel, leave, or say something aloud.",
        guild_only=True,
    )
    greeting = app_commands.Group(
        name="greeting",
        description="Custom lines Bob says when someone joins his voice channel.",
        parent=voice,
    )

    async def _voice_access(self, interaction: discord.Interaction) -> bool:
        if has_access(interaction.user):
            return True
        await interaction.response.send_message(
            "Access Denied — only the Owner, Fire Lord and people on Bob's access list can control his voice.",
            ephemeral=True,
        )
        return False

    @voice.command(name="join", description="Bob joins the voice channel you're in.")
    async def voice_join(self, interaction: discord.Interaction) -> None:
        if not await self._voice_access(interaction):
            return
        # Connecting can take several seconds.
        await interaction.response.defer(ephemeral=True)
        reply = await voice_feature.join_member_channel(self.voice_manager, interaction.user)
        await interaction.edit_original_response(content=reply)

    @voice.command(name="leave", description="Bob leaves voice on this server.")
    async def voice_leave(self, interaction: discord.Interaction) -> None:
        if not await self._voice_access(interaction):
            return
        reply = await voice_feature.leave_member_guild(self.voice_manager, interaction.user)
        await interaction.response.send_message(reply, ephemeral=True)

    @voice.command(name="say", description="Bob says something aloud in his voice channel.")
    @app_commands.describe(text="What Bob should say.")
    async def voice_say(
        self, interaction: discord.Interaction, text: app_commands.Range[str, 1, 600]
    ) -> None:
        if not await self._voice_access(interaction):
            return
        # Generating speech can take several seconds.
        await interaction.response.defer(ephemeral=True)
        reply = await voice_feature.say_in_voice(self.voice_manager, interaction.user, text)
        await interaction.edit_original_response(content=reply)

    @voice.command(name="status", description="Where Bob is in voice and what happened recently.")
    async def voice_status(self, interaction: discord.Interaction) -> None:
        if not await self._voice_access(interaction):
            return
        await interaction.response.send_message(
            voice_feature.voice_status_report(self.voice_manager, interaction.guild), ephemeral=True
        )

    # Anyone on the access list can set their own greeting; the Owner and Fire
    # Lord can set anyone's. Greetings only ever play for people with access,
    # so setting one for someone without it is refused rather than saved
    # uselessly.

    @greeting.command(name="set", description="Set someone's greeting (your own, or anyone's if you're Owner/Fire Lord).")
    @app_commands.describe(
        user="Who this greeting is for.",
        line="What Bob says. {name} is replaced with their name.",
    )
    async def greeting_set(
        self, interaction: discord.Interaction, user: discord.Member,
        line: app_commands.Range[str, 1, greetings.MAX_GREETING_LENGTH],
    ) -> None:
        if not await self._voice_access(interaction):
            return
        await interaction.response.send_message(self._set_greeting(interaction.user, user, line), ephemeral=True)

    def _set_greeting(self, actor: discord.Member, target: discord.Member, line: str) -> str:
        if target.id != actor.id and not can_manage(actor):
            return "You can only change your own greeting — the Owner and Fire Lord can change anyone's."
        if target.bot:
            return "Bots aren't greeted."
        if not has_access(target):
            return (
                f"{target.mention} isn't on my access list, and I only greet people who are — "
                "so I haven't saved it."
            )
        line = " ".join(line.split())
        if not line:
            return "The greeting can't be empty."
        if not greetings.set_greeting(target.id, line):
            return "I couldn't save that to disk — check the logs."
        preview = greetings.greeting_for(target.id, target.display_name)
        return (
            f'Saved. When {target.mention} joins my voice channel I\'ll say: "{preview}"\n'
            "*(If Google's text-to-speech refuses the line, I'll log it and stay quiet rather than "
            "fall back to something else.)*"
        )

    @greeting.command(name="clear", description='Go back to the default greeting.')
    @app_commands.describe(user="Whose greeting to clear.")
    async def greeting_clear(self, interaction: discord.Interaction, user: discord.User) -> None:
        if not await self._voice_access(interaction):
            return
        if user.id != interaction.user.id and not can_manage(interaction.user):
            reply = "You can only change your own greeting — the Owner and Fire Lord can change anyone's."
        elif greetings.clear_greeting(user.id):
            reply = f'Cleared — {user.mention} gets "{greetings.DEFAULT_GREETING}" again.'
        else:
            reply = f"{user.mention} doesn't have a custom greeting."
        await interaction.response.send_message(reply, ephemeral=True)

    @greeting.command(name="list", description="Show everyone's custom greeting.")
    async def greeting_list(self, interaction: discord.Interaction) -> None:
        if not await self._voice_access(interaction):
            return
        entries = greetings.list_greetings()
        if not entries:
            reply = f'No custom greetings yet — everyone gets "{greetings.DEFAULT_GREETING}"'
        else:
            lines = "\n".join(f'• <@{user_id}> — "{line}"' for user_id, line in entries)
            reply = (
                f"**Custom voice greetings:**\n{lines}\n"
                f'Everyone else on the access list gets "{greetings.DEFAULT_GREETING}"'
            )
        await interaction.response.send_message(reply, ephemeral=True)
