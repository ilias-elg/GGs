"""Voice commands and the voice behaviour that runs on its own."""

import logging

import discord
from discord import app_commands
from discord.ext import commands

from .. import greetings
from .. import tone as tone_feature
from .. import voice as voice_feature
from ..ranks import can_manage, has_access
from .base import FireNationCog

logger = logging.getLogger("discord")


class VoiceCommands(FireNationCog):
    """/voice and /voice greeting, plus greeting people and reading announcements aloud."""

    @commands.Cog.listener()
    async def on_voice_state_update(
        self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState
    ) -> None:
        await voice_feature.handle_voice_state_update(self.voice_manager, member, before, after)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        await voice_feature.handle_announcement_message(self.voice_manager, message)

    # ── /voice ───────────────────────────────────────────────────────────────

    voice = app_commands.Group(
        name="voice",
        description="The Archivist's voice: join your voice channel, leave, or say something aloud.",
        guild_only=True,
    )
    greeting = app_commands.Group(
        name="greeting",
        description="Custom lines the Archivist says when someone joins his voice channel.",
        parent=voice,
    )

    async def _voice_access(self, interaction: discord.Interaction) -> bool:
        if has_access(interaction.user):
            return True
        await interaction.response.send_message(
            "Access Denied — only the Owner, Fire Lord and people on the Archivist's access list can control his voice.",
            ephemeral=True,
        )
        return False

    @voice.command(name="join", description="The Archivist joins the voice channel you're in, on any server he's on.")
    async def voice_join(self, interaction: discord.Interaction) -> None:
        if not await self._voice_access(interaction):
            return
        # Connecting can take several seconds.
        await interaction.response.defer(ephemeral=True)
        reply = await voice_feature.join_member_channel(self.voice_manager, interaction.user)
        await interaction.edit_original_response(content=reply)

    @voice.command(name="leave", description="The Archivist leaves the voice channel he's in with you.")
    async def voice_leave(self, interaction: discord.Interaction) -> None:
        if not await self._voice_access(interaction):
            return
        reply = await voice_feature.leave_member_guild(self.voice_manager, interaction.user)
        await interaction.response.send_message(reply, ephemeral=True)

    @voice.command(name="say", description="The Archivist says something aloud in his voice channel.")
    @app_commands.describe(
        text="What the Archivist should say. Capitals, !!, ?, ... and emoji shape how he says it.",
        tone="Force a tone instead of letting the Archivist read it from the text.",
    )
    @app_commands.choices(tone=[
        app_commands.Choice(name="Neutral (ignore the text's tone)", value="neutral"),
        *(app_commands.Choice(name=t.label, value=t.name) for t in tone_feature.TONES.values()),
    ])
    async def voice_say(
        self, interaction: discord.Interaction, text: app_commands.Range[str, 1, 600], tone: str | None = None
    ) -> None:
        if not await self._voice_access(interaction):
            return
        # Generating speech can take several seconds.
        await interaction.response.defer(ephemeral=True)
        reply = await voice_feature.say_in_voice(self.voice_manager, interaction.user, text, tone)
        await interaction.edit_original_response(content=reply)

    @voice.command(name="status", description="Where the Archivist is in voice and what happened recently.")
    async def voice_status(self, interaction: discord.Interaction) -> None:
        if not await self._voice_access(interaction):
            return
        await interaction.response.send_message(
            voice_feature.voice_status_report(self.voice_manager, interaction.guild, interaction.user),
            ephemeral=True,
        )

    # Anyone on the access list can set their own greeting; the Owner and Fire
    # Lord can set anyone's, including people who are not on the list — a
    # custom greeting gets them greeted, and nothing more.

    @greeting.command(name="set", description="Set someone's greeting (your own, or anyone's if you're Owner/Fire Lord).")
    @app_commands.describe(
        user="Who this greeting is for.",
        line="What the Archivist says. {name} is replaced with their name.",
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
            reply = (
                f'Cleared — {user.mention} gets "{greetings.DEFAULT_GREETING}" again.'
                if has_access(user)
                else f"Cleared — {user.mention} isn't on my access list, so they won't be greeted any more."
            )
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
