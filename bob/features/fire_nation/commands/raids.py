"""Raid merits from the Fire Nation website, and /robloxlink for names that don't match."""

import logging

import discord
from discord import app_commands
from discord.ext import tasks

import config

from .. import merit, website_raids
from ..ranks import rank_at_least
from .base import FireNationCog

logger = logging.getLogger("discord")


class RaidResultsCommands(FireNationCog):
    """Polls the website's raid results and manages Roblox ↔ Discord links."""

    async def cog_load(self) -> None:
        self.bot.add_dynamic_items(website_raids.WebsiteRaidButton)
        if config.RAID_RESULTS_API_URL:
            self.poll_raids.change_interval(seconds=max(30, config.RAID_RESULTS_POLL_SECONDS))
            self.poll_raids.start()

    async def cog_unload(self) -> None:
        self.poll_raids.cancel()

    @tasks.loop(seconds=120)
    async def poll_raids(self) -> None:
        try:
            posted = await website_raids.check_for_new_raids(self.bot)
            if posted:
                logger.info(f"Posted {posted} website raid card(s) for approval")
        except Exception as e:
            logger.error(f"Website raid check failed: {e}", exc_info=True)

    @poll_raids.before_loop
    async def _before_poll(self) -> None:
        await self.bot.wait_until_ready()

    # ── /robloxlink ──────────────────────────────────────────────────────────

    robloxlink = app_commands.Group(
        name="robloxlink",
        description="Match Roblox names on the raid results site to members. HR and above.",
        guild_only=True,
    )

    async def _officer(self, interaction: discord.Interaction) -> bool:
        if rank_at_least(interaction.user, "hr"):
            return True
        await interaction.response.send_message("Access Denied — HR and above only.", ephemeral=True)
        return False

    @robloxlink.command(name="set", description="Say which member a Roblox username on the raid site belongs to.")
    @app_commands.describe(roblox_name="Their Roblox username, exactly as the raid site shows it.", member="The member it belongs to.")
    async def robloxlink_set(
        self, interaction: discord.Interaction, roblox_name: app_commands.Range[str, 3, 32], member: discord.Member
    ) -> None:
        if not await self._officer(interaction):
            return
        name = roblox_name.strip()
        if member.bot:
            await interaction.response.send_message("Bots don't earn merits.", ephemeral=True)
            return
        await website_raids.set_link(name, member.id, interaction.user.id)
        await interaction.response.send_message(
            f"Linked — raid results for **{discord.utils.escape_markdown(name)}** now go to {member.mention}.",
            ephemeral=True,
        )

    @robloxlink.command(name="remove", description="Remove a Roblox username link.")
    @app_commands.describe(roblox_name="The Roblox username to unlink.")
    async def robloxlink_remove(self, interaction: discord.Interaction, roblox_name: str) -> None:
        if not await self._officer(interaction):
            return
        removed = await website_raids.remove_link(roblox_name.strip())
        await interaction.response.send_message(
            "Removed." if removed else "That Roblox name wasn't linked.", ephemeral=True
        )

    @robloxlink.command(name="list", description="Show every Roblox username link.")
    async def robloxlink_list(self, interaction: discord.Interaction) -> None:
        if not await self._officer(interaction):
            return
        links = await website_raids.get_links()
        if not links:
            await interaction.response.send_message(
                "No links yet — Roblox names are matched to Discord names automatically; links are only needed "
                "where they differ.",
                ephemeral=True,
            )
            return
        lines = [f"• **{discord.utils.escape_markdown(name)}** → <@{member_id}>" for name, member_id in sorted(links.items())]
        chunks = merit._chunk_lines(lines, 1900)
        await interaction.response.send_message(chunks[0], ephemeral=True)
        for chunk in chunks[1:5]:
            await interaction.followup.send(chunk, ephemeral=True)
