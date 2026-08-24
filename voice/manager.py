"""Voice channel management for Bob."""
import asyncio
import logging
import os

import discord

import config

logger = logging.getLogger("discord")


class VoiceManager:
    """
    Manages voice channel connections and audio playback.

    One VoiceManager per bot instance. Tracks the current voice client
    per guild so we can move, stop, or disconnect cleanly.
    """

    def __init__(self, bot: discord.ext.commands.Bot) -> None:
        self.bot = bot

    # ── Public interface ─────────────────────────────────────────────────────

    async def join(self, guild: discord.Guild, channel: discord.VoiceChannel) -> dict:
        """Join a voice channel, optionally playing the configured join sound."""
        try:
            vc = guild.voice_client
            if vc and vc.is_connected():
                if vc.channel.id == channel.id:
                    return {"success": True, "result": f"Already in {channel.name}."}
                await vc.move_to(channel)
                logger.info(f"Moved to voice channel: {channel.name} in {guild.name}")
            else:
                vc = await channel.connect()
                logger.info(f"Joined voice channel: {channel.name} in {guild.name}")

            # Play join sound if configured and file exists
            if config.VOICE_JOIN_SOUND_ENABLED and config.VOICE_JOIN_SOUND_PATH:
                if os.path.isfile(config.VOICE_JOIN_SOUND_PATH):
                    await self._play_file(vc, config.VOICE_JOIN_SOUND_PATH)
                else:
                    logger.warning(
                        f"VOICE_JOIN_SOUND_PATH set but file not found: {config.VOICE_JOIN_SOUND_PATH}"
                    )

            return {"success": True, "result": f"Joined {channel.name}."}
        except discord.ClientException as e:
            return {"error": f"Voice connection error: {e}"}
        except Exception as e:
            logger.error(f"VoiceManager.join error: {e}", exc_info=True)
            return {"error": f"Failed to join voice: {e}"}

    async def leave(self, guild: discord.Guild) -> dict:
        """Disconnect from the current voice channel."""
        vc = guild.voice_client
        if not vc or not vc.is_connected():
            return {"error": "Not currently in a voice channel."}
        channel_name = vc.channel.name
        await vc.disconnect()
        logger.info(f"Left voice channel: {channel_name} in {guild.name}")
        return {"success": True, "result": f"Left {channel_name}."}

    async def play(self, guild: discord.Guild, file_path: str) -> dict:
        """Play an audio file in the current voice channel."""
        vc = guild.voice_client
        if not vc or not vc.is_connected():
            return {"error": "Not connected to a voice channel."}
        if not os.path.isfile(file_path):
            return {"error": f"Audio file not found: {file_path}"}

        await self._play_file(vc, file_path)
        return {"success": True, "result": f"Playing {os.path.basename(file_path)}"}

    async def stop(self, guild: discord.Guild) -> dict:
        """Stop currently playing audio."""
        vc = guild.voice_client
        if not vc:
            return {"error": "Not in a voice channel."}
        if vc.is_playing():
            vc.stop()
            return {"success": True, "result": "Stopped audio."}
        return {"result": "Nothing is currently playing."}

    # ── Internal ─────────────────────────────────────────────────────────────

    async def _play_file(self, vc: discord.VoiceClient, file_path: str) -> None:
        """Play a file, stopping any current playback first."""
        if vc.is_playing():
            vc.stop()
            # Small gap so the previous audio finishes stopping
            await asyncio.sleep(0.25)

        try:
            source = discord.FFmpegPCMAudio(
                file_path,
                options="-vn",  # no video
            )
            vc.play(source)
            logger.info(f"Playing audio: {file_path}")
        except Exception as e:
            logger.error(f"Audio playback error: {e}")
