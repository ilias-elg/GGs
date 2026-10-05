"""Voice channel management for Bob."""
import asyncio
import io
import logging
import os
import time

import discord

import config

logger = logging.getLogger("discord")

MAX_QUEUED_LINES = 5
ALONE_TIMEOUT_SECONDS = 15 * 60
# Discord voice connections blip briefly (someone joining, voice server
# moves). Lines wait this long for the connection to come back instead of
# failing — but no longer, so one bad moment can't jam the queue.
READY_WAIT_SECONDS = 10
# A clip that hasn't finished this long after its own length has stalled.
PLAYBACK_GRACE_SECONDS = 15
MAX_EVENTS = 15


class _GuildVoice:
    """Per-server speech queue state."""

    def __init__(self) -> None:
        self.lock = asyncio.Lock()
        self.pending = 0
        self.alone_task: asyncio.Task | None = None


class VoiceManager:
    """
    Manages voice channel connections and audio playback.

    One VoiceManager per bot instance. Each server gets one connection and a
    speech queue, so an announcement, a greeting and a "say that out loud"
    never talk over each other — they play in order.
    """

    def __init__(self, bot: discord.ext.commands.Bot) -> None:
        self.bot = bot
        self._guilds: dict[int, _GuildVoice] = {}
        # The last few things that happened in voice, per server, so "voice
        # status" and /diagnostics can show exactly why something did or
        # didn't play instead of anyone having to guess.
        self._events: dict[int, list[tuple[int, str]]] = {}

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
                vc = await channel.connect(self_deaf=True)
                logger.info(f"Joined voice channel: {channel.name} in {guild.name}")
            self.event(guild.id, f"joined {channel.name}")

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
            self.event(guild.id, f"failed to join {channel.name}: {e}")
            return {"error": f"Voice connection error: {e}"}
        except Exception as e:
            logger.error(f"VoiceManager.join error: {e}", exc_info=True)
            self.event(guild.id, f"failed to join {channel.name}: {e}")
            return {"error": f"Failed to join voice: {e}"}

    async def leave(self, guild: discord.Guild, reason: str = "asked to leave") -> dict:
        """Disconnect from the current voice channel."""
        vc = guild.voice_client
        if not vc or not vc.is_connected():
            return {"error": "Not currently in a voice channel."}
        channel_name = vc.channel.name
        self.event(guild.id, f"left voice: {reason}")
        self.forget(guild.id)
        await vc.disconnect()
        logger.info(f"Left voice channel: {channel_name} in {guild.name}")
        return {"success": True, "result": f"Left {channel_name}."}

    async def leave_all(self) -> None:
        for vc in list(self.bot.voice_clients):
            await self.leave(vc.guild, "bot shutting down")

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

    # ── Status ───────────────────────────────────────────────────────────────

    def current_channel(self, guild: discord.Guild):
        """The voice channel Bob is in on this server, if any."""
        vc = guild.voice_client
        return vc.channel if vc and vc.is_connected() else None

    def alone_timer_running(self, guild_id: int) -> bool:
        state = self._guilds.get(guild_id)
        return bool(state and state.alone_task)

    def event(self, guild_id: int, text: str) -> None:
        events = self._events.setdefault(guild_id, [])
        events.append((int(time.time()), text))
        del events[:-MAX_EVENTS]
        logger.info(f"Voice [{guild_id}]: {text}")

    def recent_events(self, guild_id: int, limit: int = 10) -> list[tuple[int, str]]:
        return self._events.get(guild_id, [])[-limit:]

    def forget(self, guild_id: int) -> None:
        """Drops queue state for a server Bob is no longer in voice on."""
        state = self._guilds.pop(guild_id, None)
        if state and state.alone_task:
            state.alone_task.cancel()

    # ── Speech ───────────────────────────────────────────────────────────────

    async def speak(
        self, guild: discord.Guild, text: str, *, cache: bool = False, label: str | None = None
    ) -> tuple[bool, str]:
        """
        Speaks a line in the server's voice channel. Speech is generated
        straight away (so it's ready sooner) but played strictly in order.
        Returns (True, "") once the line has finished playing — or
        (False, reason) when it couldn't be.
        """
        from bob.features.fire_nation import tts

        label = label or f'"{text[:60]}{"…" if len(text) > 60 else ""}"'
        if not self.current_channel(guild):
            return False, "I'm not in a voice channel on this server."
        if not text.strip():
            return False, "There was nothing to say."
        state = self._guilds.setdefault(guild.id, _GuildVoice())
        if state.pending >= MAX_QUEUED_LINES:
            self.event(guild.id, f"skipped {label}: queue full")
            return False, "I've already got several lines queued up — give me a moment."

        state.pending += 1
        synthesis = asyncio.create_task(tts.synthesize_speech(text, cache=cache))
        try:
            async with state.lock:
                speech = await synthesis
                if not speech.ok:
                    self.event(guild.id, f"couldn't generate {label}: {speech.reason}")
                    if speech.quota_hit:
                        return False, "my voice quota with Google is used up for now."
                    return False, f"I couldn't generate the speech ({speech.reason})."

                # The connection may have gone away while the audio was being generated.
                vc = guild.voice_client
                if not vc:
                    return False, "I left the voice channel before I could say it."
                for _ in range(READY_WAIT_SECONDS * 2):
                    if vc.is_connected():
                        break
                    await asyncio.sleep(0.5)
                else:
                    self.event(guild.id, f"dropped {label}: voice connection wasn't ready")
                    return False, "my voice connection is reconnecting — try again in a moment."

                audio = tts.to_discord_pcm(speech.pcm, speech.sample_rate)
                duration = len(speech.pcm) / (speech.sample_rate * 2)
                finished = asyncio.Event()
                loop = asyncio.get_running_loop()
                if vc.is_playing():
                    vc.stop()
                    await asyncio.sleep(0.25)
                vc.play(
                    discord.PCMAudio(io.BytesIO(audio)),
                    after=lambda _err: loop.call_soon_threadsafe(finished.set),
                )
                try:
                    await asyncio.wait_for(finished.wait(), duration + PLAYBACK_GRACE_SECONDS)
                except asyncio.TimeoutError:
                    vc.stop()
                    self.event(guild.id, f"stopped {label}: playback stalled")
                    return False, "playback stalled, so I stopped it."
                self.event(guild.id, f"said {label}{' (cached)' if speech.cached else ''}")
                return True, ""
        except Exception as e:
            logger.warning(f"Voice line failed: {e}", exc_info=True)
            self.event(guild.id, f"error while saying {label}: {e}")
            return False, f"something went wrong while speaking ({e})."
        finally:
            state.pending -= 1

    # ── Auto-leave ───────────────────────────────────────────────────────────

    def update_alone_timer(self, guild: discord.Guild) -> None:
        """
        Leaves after 15 minutes with no people in the channel (bots don't
        count), so Bob doesn't sit in an empty channel indefinitely — but
        stays long enough that people stepping out and back in still find him.
        """
        channel = self.current_channel(guild)
        if not channel:
            self.forget(guild.id)
            return
        state = self._guilds.setdefault(guild.id, _GuildVoice())
        humans = sum(1 for m in channel.members if not m.bot)

        if humans > 0:
            if state.alone_task:
                state.alone_task.cancel()
                state.alone_task = None
                self.event(guild.id, "someone's back — cancelled auto-leave")
        elif not state.alone_task:
            minutes = ALONE_TIMEOUT_SECONDS // 60
            self.event(guild.id, f"channel empty — leaving in {minutes} minutes unless someone joins")
            state.alone_task = asyncio.create_task(self._leave_when_alone(guild, minutes))

    async def _leave_when_alone(self, guild: discord.Guild, minutes: int) -> None:
        await asyncio.sleep(ALONE_TIMEOUT_SECONDS)
        state = self._guilds.get(guild.id)
        if state:
            state.alone_task = None  # don't let leave() cancel the task it's running in
        await self.leave(guild, f"channel was empty for {minutes} minutes")

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


_instance: VoiceManager | None = None


def get_voice_manager(bot) -> VoiceManager:
    """The bot's single VoiceManager, shared by the AI tools and the voice commands."""
    global _instance
    if _instance is None:
        _instance = VoiceManager(bot)
    return _instance
