"""Raid merits from the Fire Nation website, and /robloxlink for names that don't match."""

import asyncio
import logging

import discord
from discord import app_commands
from discord.ext import commands, tasks

import config

from .. import merit, website_raids
from ..ranks import get_rank, rank_at_least
from .base import FireNationCog

logger = logging.getLogger("discord")

# After a "Raid Concluded" post the website usually has the results within a
# few minutes; it is asked at these delays (minutes) until the raid shows up.
AFTER_CONCLUSION_MINUTES = (2, 5, 10, 20, 40)


class RaidResultsCommands(FireNationCog):
    """Checks the website's raid results when a raid has happened, and manages Roblox ↔ Discord links."""

    def __init__(self, bot) -> None:
        super().__init__(bot)
        self._after_conclusion: asyncio.Task | None = None

    async def cog_load(self) -> None:
        self.bot.add_dynamic_items(website_raids.WebsiteRaidButton)
        if config.RAID_RESULTS_API_URL and config.RAID_RESULTS_POLL_SECONDS > 0:
            self.fallback_check.change_interval(seconds=max(600, config.RAID_RESULTS_POLL_SECONDS))
            self.fallback_check.start()

    async def cog_unload(self) -> None:
        self.fallback_check.cancel()
        if self._after_conclusion:
            self._after_conclusion.cancel()

    async def _check(self, why: str) -> int:
        try:
            posted = await website_raids.check_for_new_raids(self.bot)
            if posted:
                logger.info(f"Posted {posted} website raid card(s) for approval ({why})")
            return posted
        except Exception as e:
            logger.error(f"Website raid check failed ({why}): {e}", exc_info=True)
            return 0

    # A slow safety net for raids nobody concluded in Discord. Its first run
    # is at startup, which also picks up anything missed while offline.
    @tasks.loop(hours=6)
    async def fallback_check(self) -> None:
        await self._check("scheduled check")

    @fallback_check.before_loop
    async def _before_fallback(self) -> None:
        await self.bot.wait_until_ready()

    async def _watch_after_conclusion(self) -> None:
        waited = 0
        for minutes in AFTER_CONCLUSION_MINUTES:
            await asyncio.sleep((minutes - waited) * 60)
            waited = minutes
            if await self._check(f"{minutes} min after a raid conclusion"):
                return

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if not config.RAID_RESULTS_API_URL or message.channel.id not in config.MERIT_CONCLUSION_CHANNEL_IDS:
            return
        if not website_raids.is_raid_conclusion(message.content):
            return
        # One watch at a time: a second post while one runs changes nothing.
        if self._after_conclusion is None or self._after_conclusion.done():
            self._after_conclusion = asyncio.create_task(self._watch_after_conclusion())
            logger.info("Raid concluded — checking the website for its results over the next 40 minutes")

    # ── /raidcheck ───────────────────────────────────────────────────────────

    @app_commands.command(name="raidcheck", description="Check the website for new raid results now. Advisor and above.")
    @app_commands.guild_only()
    async def raidcheck(self, interaction: discord.Interaction) -> None:
        try:
            merit.assert_can_award(get_rank(interaction.user), "raid")
        except merit.MeritError as e:
            await interaction.response.send_message(str(e), ephemeral=True)
            return
        if not config.RAID_RESULTS_API_URL:
            await interaction.response.send_message("Reading raid results from the website is turned off.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        posted = await self._check(f"/raidcheck by {interaction.user}")
        await interaction.edit_original_response(
            content=f"Posted **{posted}** new raid card{'' if posted == 1 else 's'} in the owner log channel."
            if posted else "No new raids on the website."
        )

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
