"""/inactivity — warn members who have gained no merits lately, then remove those who stay inactive."""

import logging

import discord
from discord import app_commands
from discord.ext import commands

from .. import inactivity as inact
from ..ranks import can_manage
from .base import FireNationCog

logger = logging.getLogger("discord")

DENIED = "Access Denied — only the Owner or Fire Lord can manage inactive members."
_SHOWN = 40


def _names(members: list[discord.Member], limit: int = _SHOWN) -> str:
    text = "\n".join(f"• {m.mention} ({discord.utils.escape_markdown(str(m))})" for m in members[:limit])
    if len(members) > limit:
        text += f"\n…and {len(members) - limit} more"
    return text or "—"


class ConfirmView(discord.ui.View):
    """One-shot Yes/Cancel; runs `action` (a coroutine function taking the button interaction) on Yes."""

    def __init__(self, interaction: discord.Interaction, label: str, action) -> None:
        super().__init__(timeout=60)
        self.interaction = interaction
        self.action = action
        self.done = False
        self.confirm.label = label

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.interaction.user.id

    @discord.ui.button(style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.done = True
        self.stop()
        await interaction.response.defer()
        try:
            await self.action(interaction)
        except Exception as e:
            logger.error(f"/inactivity action failed: {e}", exc_info=True)
            await interaction.edit_original_response(content="❌ That didn't go through. Nothing more was done.", view=None)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.done = True
        self.stop()
        await interaction.response.edit_message(content="❌ Cancelled. Nobody was contacted or removed.", view=None)

    async def on_timeout(self) -> None:
        if not self.done:
            try:
                await self.interaction.edit_original_response(content="⏱️ Confirmation timed out. Cancelled.", view=None)
            except discord.HTTPException:
                pass


class InactivityCommands(FireNationCog):
    """The /inactivity group. Owner and Fire Lord only."""

    inactivity = app_commands.Group(
        name="inactivity", description="Warn and remove members with no recent merits.", guild_only=True
    )

    async def _guard(self, interaction: discord.Interaction) -> bool:
        if can_manage(interaction.user):
            return True
        await interaction.response.send_message(DENIED)
        return False

    @inactivity.command(name="preview", description="List who has gained no merits in the last N days. Changes nothing.")
    @app_commands.describe(days="Look-back window in days (default 30)")
    async def preview(self, interaction: discord.Interaction, days: app_commands.Range[int, 1, 365] = inact.DEFAULT_DAYS) -> None:
        if not await self._guard(interaction):
            return
        await interaction.response.defer()
        members = await inact.find_inactive(interaction.guild, days)
        await interaction.edit_original_response(
            content=(
                f"**{len(members)}** member(s) have gained no merits in the last **{days} days** "
                f"(bots, rank holders, the exempt list and recent joiners are left out):\n{_names(members)}"
            )
        )

    @inactivity.command(name="warn", description="DM inactive members a warning and start their grace period.")
    @app_commands.describe(
        days="No merits in this many days counts as inactive (default 30)",
        grace_days="How long they have to earn a merit before they can be removed (default 7)",
    )
    async def warn(
        self,
        interaction: discord.Interaction,
        days: app_commands.Range[int, 1, 365] = inact.DEFAULT_DAYS,
        grace_days: app_commands.Range[int, 1, 60] = inact.DEFAULT_GRACE_DAYS,
    ) -> None:
        if not await self._guard(interaction):
            return
        await interaction.response.defer()
        guild = interaction.guild
        members = await inact.find_inactive(guild, days)
        if not members:
            await interaction.edit_original_response(content=f"Nobody has gone {days} days without a merit. Nothing to do.")
            return

        async def run(button_interaction: discord.Interaction) -> None:
            delivered, failed = await inact.warn_members(guild, members, days, grace_days, interaction.user)
            await inact.audit(self.bot, "Inactivity warnings sent", delivered + failed, interaction.user,
                              f"Grace period {grace_days} days.")
            text = f"✅ Warned **{len(delivered)}** member(s) by DM. They have **{grace_days} days** to earn a merit."
            if failed:
                text += (
                    f"\n\n⚠️ **{len(failed)}** have DMs closed, so they were **not** properly warned and "
                    f"won't be removed unless you pass `include_no_dm` to `/inactivity kick`:\n{_names(failed)}"
                )
            await interaction.edit_original_response(content=text, view=None)

        await interaction.edit_original_response(
            content=(
                f"⚠️ **Warn {len(members)} member(s)** who have gained no merits in **{days} days**? "
                f"Each gets a DM and **{grace_days} days** to earn one. Nobody is removed by this step.\n{_names(members)}"
            ),
            view=ConfirmView(interaction, "Yes, send the warnings", run),
        )

    @inactivity.command(name="status", description="Show who has been warned and when their grace period ends.")
    async def status(self, interaction: discord.Interaction) -> None:
        if not await self._guard(interaction):
            return
        await interaction.response.defer()
        rows = await inact.pending_warnings(interaction.guild.id)
        lines = [
            f"• <@{r['member_id']}> — ends <t:{int(r['deadline'].timestamp())}:R>"
            + ("" if r["dm_delivered"] else " ⚠️ DM not delivered")
            for r in rows[:_SHOWN]
        ]
        if len(rows) > _SHOWN:
            lines.append(f"…and {len(rows) - _SHOWN} more")
        exempt = await inact.list_exempt(interaction.guild.id)
        await interaction.edit_original_response(
            content=(
                f"**{len(rows)}** pending warning(s):\n" + ("\n".join(lines) or "—")
                + f"\n\n**Exempt:** " + (" ".join(f"<@{i}>" for i in exempt) or "nobody")
            )
        )

    @inactivity.command(name="kick", description="Remove warned members whose grace period is over and who still have no merits.")
    @app_commands.describe(
        days="The window you warned with (only used in the removal reason; default 30)",
        include_no_dm="Also remove people whose DMs were closed, so the warning never reached them",
    )
    async def kick(
        self,
        interaction: discord.Interaction,
        days: app_commands.Range[int, 1, 365] = inact.DEFAULT_DAYS,
        include_no_dm: bool = False,
    ) -> None:
        if not await self._guard(interaction):
            return
        await interaction.response.defer()
        guild = interaction.guild
        due, skipped = await inact.due_for_removal(guild, include_undelivered=include_no_dm)
        notes = []
        if skipped["redeemed"]:
            notes.append(f"{len(skipped['redeemed'])} earned a merit since their warning and were cleared.")
        if skipped["still_waiting"]:
            notes.append(f"{len(skipped['still_waiting'])} are still inside their grace period.")
        if skipped["no_dm"]:
            notes.append(f"{len(skipped['no_dm'])} never received the DM and were skipped (`include_no_dm` to override).")
        note = ("\n\n" + " ".join(notes)) if notes else ""
        if not due:
            await interaction.edit_original_response(content="Nobody is due for removal." + note)
            return

        async def run(button_interaction: discord.Interaction) -> None:
            removed, failed = await inact.remove_members(guild, due, interaction.user, days)
            await inact.audit(self.bot, "Inactive members removed", removed, interaction.user)
            text = f"✅ Removed **{len(removed)}** inactive member(s)."
            if failed:
                text += "\n\n⚠️ Could not remove:\n" + "\n".join(f"• {m.mention} — {why}" for m, why in failed[:_SHOWN])
            await interaction.edit_original_response(content=text, view=None)

        await interaction.edit_original_response(
            content=(
                f"⚠️ **Remove {len(due)} member(s)?** Their grace period is over and they gained no merits. "
                f"They can rejoin with an invite.\n{_names(due)}{note}"
            ),
            view=ConfirmView(interaction, f"Yes, remove {len(due)}", run),
        )

    @inactivity.command(name="cancel", description="Cancel warnings: everyone's, or just one member's.")
    @app_commands.describe(member="Leave empty to cancel every pending warning")
    async def cancel(self, interaction: discord.Interaction, member: discord.Member | None = None) -> None:
        if not await self._guard(interaction):
            return
        count = await inact.clear_warnings(interaction.guild.id, member.id if member else None)
        await interaction.response.send_message(f"Cancelled {count} pending warning(s).")

    @inactivity.command(name="exempt", description="Never include a member in inactivity cleanups (e.g. on a break).")
    @app_commands.describe(member="The member", exempt="True to protect them, False to remove the protection")
    async def exempt(self, interaction: discord.Interaction, member: discord.Member, exempt: bool = True) -> None:
        if not await self._guard(interaction):
            return
        changed = await inact.set_exempt(interaction.guild.id, member.id, exempt, interaction.user.id)
        if exempt:
            await inact.clear_warnings(interaction.guild.id, member.id)
        verb = "is now exempt from" if exempt else "is no longer exempt from"
        await interaction.response.send_message(
            f"{member.mention} {verb} inactivity cleanups." if changed else "No change — they were already that way.",
        )
