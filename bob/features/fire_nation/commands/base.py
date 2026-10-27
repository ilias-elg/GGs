"""
What every Fire Nation command cog shares: access to the bot and the voice
manager, and the error handler that tells the Owner instead of the channel.
"""

import logging

import discord
from discord import app_commands
from discord.ext import commands

from bob.owner_alerts import report_error
from bob.voice.manager import get_voice_manager

logger = logging.getLogger("discord")


class FireNationCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.voice_manager = get_voice_manager(bot)

    async def cog_app_command_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ) -> None:
        logger.error(f"/{interaction.command.qualified_name if interaction.command else '?'} failed: {error}", exc_info=error)
        name = interaction.command.qualified_name if interaction.command else "?"
        await report_error(
            self.bot, getattr(error, "original", error), what=f"running /{name}",
            user=interaction.user, channel=interaction.channel,
        )
        # Only the person who ran it sees this, and it carries no details —
        # without it the command would just hang on "thinking…".
        reply = "That didn't go through. The Owner has been told."
        try:
            if interaction.response.is_done():
                await interaction.edit_original_response(content=reply, view=None)
            else:
                await interaction.response.send_message(reply, ephemeral=True)
        except discord.HTTPException:
            pass
