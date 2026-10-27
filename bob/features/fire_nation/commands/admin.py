"""Owner and officer tools: rank roles, knowledge, access list, cleanup, diagnostics."""

import io
import logging
import re
from datetime import datetime, timedelta, timezone

import discord
from discord import app_commands
from discord.ext import commands

import config

from .. import diagnostics, knowledge, presence, tts
from ..ranks import can_manage, rank_at_least
from .base import FireNationCog

logger = logging.getLogger("discord")


class AdminCommands(FireNationCog):
    """Rank roles, the knowledge base, the access list, /cleanup and /diagnostics."""

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        await presence.start(self.bot)

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
                content=f"The {role_name} role already exists: {existing.mention}. The Archivist will recognize it."
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
        name="reloadknowledge", description="Reload the Fire Nation knowledge file without restarting the Archivist."
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

    # ── /access ──────────────────────────────────────────────────────────────

    access = app_commands.Group(
        name="access", description="Who the Archivist talks to. Owner and Fire Lord only.", guild_only=True
    )

    async def _change_access(self, interaction: discord.Interaction, user: discord.Member, grant: bool) -> None:
        # Imported here: the tools package imports this feature, so a top-level import would be circular.
        from bob.tools.fire_nation_tools import change_access
        result = change_access(interaction.user, user, grant)
        await interaction.response.send_message(result.get("result") or result["error"], ephemeral=True)

    @access.command(name="add", description="Let someone talk to the Archivist.")
    @app_commands.describe(user="The member the Archivist should talk to.")
    async def access_add(self, interaction: discord.Interaction, user: discord.Member) -> None:
        await self._change_access(interaction, user, True)

    @access.command(name="remove", description="Stop the Archivist talking to someone.")
    @app_commands.describe(user="The member the Archivist should stop responding to.")
    async def access_remove(self, interaction: discord.Interaction, user: discord.Member) -> None:
        await self._change_access(interaction, user, False)

    @access.command(name="list", description="Show everyone on the Archivist's access list.")
    async def access_list(self, interaction: discord.Interaction) -> None:
        from bob.tools.fire_nation_tools import access_list_text
        if not can_manage(interaction.user):
            await interaction.response.send_message(
                "Only the Owner or Fire Lord can see the access list.", ephemeral=True
            )
            return
        await interaction.response.send_message(
            access_list_text(interaction.guild), ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    # ── /cleanup ─────────────────────────────────────────────────────────────

    ALL_SERVERS = "all"

    async def _delete_own_messages(self, channel, since: datetime) -> tuple[int, int]:
        """Deletes Bob's own messages in one channel since `since`. Returns (deleted, failed)."""
        # Nothing has been posted there since the cut-off: skip the request.
        if channel.last_message_id and discord.utils.snowflake_time(channel.last_message_id) < since:
            return 0, 0
        deleted = failed = 0
        try:
            # Only his own messages, one at a time: a bot may always delete
            # those, whereas bulk deletion needs Manage Messages.
            async for message in channel.history(after=since, limit=None):
                if message.author.id != self.bot.user.id:
                    continue
                try:
                    await message.delete()
                    deleted += 1
                except discord.HTTPException:
                    failed += 1
        except discord.HTTPException:
            pass
        return deleted, failed

    def _find_channel(self, guild: discord.Guild, text: str):
        """
        A channel in any server Bob is in, from a #mention, an ID, or a link to
        the channel or to a message in it. Typed as text rather than picked
        from Discord's list, because that list only offers the current server.
        """
        text = text.strip()
        link = re.search(r"channels/\d+/(\d+)", text)
        mention = re.fullmatch(r"<#(\d+)>", text)
        channel_id = link.group(1) if link else mention.group(1) if mention else text if text.isdigit() else None
        if channel_id:
            found = self.bot.get_channel(int(channel_id))
        else:
            found = discord.utils.get(guild.text_channels, name=text.lstrip("#").lower())
        return found if hasattr(found, "history") and getattr(found, "guild", None) else None

    async def _cleanup_server_choices(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        if not can_manage(interaction.user):
            return []
        choices = [app_commands.Choice(name="All servers", value=self.ALL_SERVERS)] + [
            app_commands.Choice(name=guild.name[:100], value=str(guild.id)) for guild in self.bot.guilds
        ]
        return [c for c in choices if current.lower() in c.name.lower()][:25]

    @app_commands.command(
        name="cleanup", description="Delete the Archivist's own recent messages, here or in any server. Owner and Fire Lord only."
    )
    @app_commands.guild_only()
    @app_commands.describe(
        minutes="How far back to go. Default: 10.",
        channel="A channel in ANY server: #mention, channel ID, or a message link. Default: this channel.",
        server="Clean every channel of another server (or all servers) from here.",
    )
    @app_commands.autocomplete(server=_cleanup_server_choices)
    async def cleanup(
        self, interaction: discord.Interaction,
        minutes: app_commands.Range[int, 1, 120] = 10,
        channel: str | None = None,
        server: str | None = None,
    ) -> None:
        if not can_manage(interaction.user):
            await interaction.response.send_message(
                "Access Denied — only the Owner or Fire Lord can clean up my messages.", ephemeral=True
            )
            return
        if server and channel:
            await interaction.response.send_message(
                "Pick either a channel or a server, not both.", ephemeral=True
            )
            return

        span = f"the last {minutes} minute{'' if minutes == 1 else 's'}"
        since = datetime.now(timezone.utc) - timedelta(minutes=minutes)

        if server:
            guilds = (
                list(self.bot.guilds) if server == self.ALL_SERVERS
                else [g for g in self.bot.guilds if str(g.id) == server or g.name.lower() == server.lower()]
            )
            if not guilds:
                await interaction.response.send_message(
                    "I'm not in a server by that name — pick one from the list.", ephemeral=True
                )
                return
            await interaction.response.defer(ephemeral=True)
            lines, total = [], 0
            for guild in guilds:
                deleted = failed = 0
                for target in guild.text_channels:
                    perms = target.permissions_for(guild.me)
                    if not (perms.view_channel and perms.read_message_history):
                        continue
                    d, f = await self._delete_own_messages(target, since)
                    deleted, failed = deleted + d, failed + f
                total += deleted
                lines.append(
                    f"• **{discord.utils.escape_markdown(guild.name)}** — {deleted} deleted"
                    + (f", {failed} could not be deleted" if failed else "")
                )
            logger.info(f"/cleanup by {interaction.user.id}: {total} of my messages across {len(guilds)} server(s), {minutes} min")
            await interaction.edit_original_response(
                content=f"Cleaned up my messages from {span}:\n" + "\n".join(lines)
            )
            return

        target = interaction.channel
        if channel:
            target = self._find_channel(interaction.guild, channel)
            if target is None:
                await interaction.response.send_message(
                    "I couldn't find that channel in any server I'm in. Use a #mention, the channel's ID, "
                    "or a link to a message in it.",
                    ephemeral=True,
                )
                return
        await interaction.response.defer(ephemeral=True)
        perms = target.permissions_for(target.guild.me)
        if not (perms.view_channel and perms.read_message_history):
            await interaction.edit_original_response(
                content=f"I can't read the message history in {target.mention}, so I can't find my messages there."
            )
            return
        deleted, failed = await self._delete_own_messages(target, since)
        logger.info(f"/cleanup by {interaction.user.id}: deleted {deleted} of my messages in {target.id} ({minutes} min)")
        where = "" if target.id == interaction.channel_id else f" in {target.mention}"
        if target.guild.id != interaction.guild_id:
            where += f" ({discord.utils.escape_markdown(target.guild.name)})"
        if not deleted and not failed:
            reply = f"I haven't posted anything{where} in {span}."
        else:
            reply = (
                f"Deleted **{deleted}** of my message{'' if deleted == 1 else 's'}{where} from {span}."
                + (f" {failed} could not be deleted." if failed else "")
            )
        await interaction.edit_original_response(content=reply)

    # ── /diagnostics ─────────────────────────────────────────────────────────

    @app_commands.command(
        name="diagnostics",
        description="Check the Archivist's AI access, server permissions, voice and database. Owner/Fire Lord only.",
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
