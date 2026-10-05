"""
Checks behind /diagnostics. Every check returns lines of (ok, text), where ok
is True (✅ working), False (❌ needs fixing — the fix is in the text) or None
(ℹ️ info / not tested). Each section reports its own failure rather than
sinking the whole report.
"""

import logging
import time
from datetime import datetime, timezone

import discord
import httpx

import config

from . import merit, tts

logger = logging.getLogger("discord")

CheckLine = tuple[bool | None, str]

# ─── AI provider ──────────────────────────────────────────────────────────────


async def run_ai_checks() -> list[CheckLine]:
    """One tiny real chat request — proves the configured provider answers."""
    from bob.ai import create_provider

    label = f"`{config.AI_PROVIDER}` / `{config.get_model()}`"
    started = time.monotonic()
    try:
        answer = await create_provider().simple_complete(
            system="You are a connectivity check.",
            user="Reply with the single word: OK",
            max_tokens=16,
            temperature=0,
        )
    except Exception as e:
        return [(False, f"Chat test ({label}) failed: {str(e)[:200]} → check the API key and AI_MODEL.")]
    took = int((time.monotonic() - started) * 1000)
    if not (answer or "").strip():
        return [(None, f"Chat test ({label}): the provider answered but sent no text ({took} ms).")]
    return [(True, f"Chat test: {label} answered a real request ({took} ms).")]


# ─── Google text-to-speech ────────────────────────────────────────────────────


def _describe_google_failure(label: str, status: int, body: object) -> CheckLine:
    if status == 429:
        return False, f"{label}: quota exhausted right now (429). Google says: {tts.google_error_message(body)}"
    if tts.error_reason(body) == "API_KEY_INVALID":
        return False, f"{label}: the API key is invalid → replace GOOGLE_API_KEY."
    if status == 403:
        return False, (
            f"{label}: permission denied (403) — the key's project may not have the Generative Language "
            f"API enabled, or the key is restricted. Google says: {tts.google_error_message(body)}"
        )
    return False, f"{label}: failed ({status}) — {tts.google_error_message(body)}"


async def run_google_checks(test_tts: bool, voice: str | None) -> tuple[list[CheckLine], bytes | None]:
    """Returns the check lines and, when a test clip was generated, its WAV bytes."""
    api_key = config.GOOGLE_API_KEY
    if not api_key:
        return [(False, "GOOGLE_API_KEY is not set → Bob can join voice but can't speak.")], None

    lines: list[CheckLine] = []
    try:
        # Key + model list (free — doesn't touch the generation quota)
        status, body, models = await tts.list_models(api_key)
        if status != 200:
            return [_describe_google_failure("API key", status, body)], None
        lines.append((True, f"API key works — it can see {len(models)} models."))

        model = config.TTS_MODEL or tts.pick_tts_model(models)
        if not model:
            lines.append((False, "No text-to-speech models are available to this key → voice lines can't be generated."))
            return lines, None
        if not test_tts:
            lines.append((
                None,
                f"Text-to-speech model available (`{model}`). Run `/diagnostics tts:True` to generate a real test clip.",
            ))
            return lines, None

        voice = tts.resolve_voice(voice)
        label = f"Voice test ({model}, voice {voice})"
        status, body = await tts.google_fetch(
            f"models/{model}:generateContent",
            api_key,
            tts.build_tts_request("Systems check complete. All voice systems nominal.", voice),
        )
        if status != 200:
            lines.append(_describe_google_failure(label, status, body))
            return lines, None
        audio = tts.extract_audio(body)
        if not audio:
            lines.append((False, f"{label}: Google answered but sent no audio back."))
            return lines, None
        pcm, rate = audio
        seconds = len(pcm) / (rate * 2)
        lines.append((True, f"{label}: generated {seconds:.1f}s of speech — clip attached, have a listen."))
        return lines, tts.pcm_to_wav(pcm, rate)
    except httpx.TimeoutException:
        lines.append((
            False,
            "Google didn't answer within 20 seconds — the host may be blocking outbound requests, "
            "or Google is having issues.",
        ))
    except httpx.HTTPError as e:
        lines.append((False, f"Couldn't reach Google: {e}"))
    return lines, None


# ─── Discord permissions ──────────────────────────────────────────────────────
# A bot can always read its own permissions, so every line here is a fact
# about this server right now — plus the exact fix when something's missing.


def _missing(perms: discord.Permissions, needed: list[str]) -> list[str]:
    return [p for p in needed if not getattr(perms, p)]


def _human(perms: list[str]) -> str:
    return ", ".join(p.replace("_", " ").title() for p in perms)


def _channel_line(label: str, channel, me: discord.Member, needed: list[str], why: str) -> CheckLine:
    lacking = _missing(channel.permissions_for(me), needed)
    if not lacking:
        return True, f"{label} <#{channel.id}>: can {why}."
    return False, (
        f"{label} <#{channel.id}>: missing **{_human(lacking)}** → give Bob's role those "
        "permissions on that channel."
    )


def run_discord_checks(guild: discord.Guild, current_channel, intents: discord.Intents) -> list[CheckLine]:
    lines: list[CheckLine] = []
    me = guild.me

    # ── Server-wide role permissions ──────────────────────────────────────────
    if me.guild_permissions.administrator:
        lines.append((
            True,
            "Bob has **Administrator** — server-wide permissions are all covered "
            "(role-hierarchy limits below still apply).",
        ))
    else:
        core = ["view_channel", "send_messages", "embed_links", "read_message_history", "attach_files"]
        lacking = _missing(me.guild_permissions, core)
        lines.append(
            (True, "Core permissions (view, send, embeds, history, files): all granted.")
            if not lacking
            else (False, f"Missing server-wide: **{_human(lacking)}** → add them to Bob's role in Server Settings → Roles.")
        )
        lines.append(
            (True, "Manage Roles: granted (needed for /createhr, /createadvisor, /createroyalty).")
            if me.guild_permissions.manage_roles
            else (False, "Missing **Manage Roles** → /createhr, /createadvisor and /createroyalty will fail until it's added.")
        )

    # ── The channel this was run in (where Bob replies) ───────────────────────
    if isinstance(current_channel, discord.abc.GuildChannel):
        lines.append(_channel_line(
            "This channel", current_channel, me,
            ["view_channel", "send_messages", "embed_links", "attach_files"],
            "chat, post embeds and attach files",
        ))

    # ── Owner audit log channel ───────────────────────────────────────────────
    if not config.OWNER_LOG_CHANNEL_ID:
        lines.append((False, "DISCORD_OWNER_LOG_CHANNEL_ID is not set → merit actions aren't being audit-logged anywhere."))
    else:
        log_channel = guild.get_channel(config.OWNER_LOG_CHANNEL_ID)
        if not log_channel:
            lines.append((
                None,
                "The audit log channel isn't in this server (it may be in another server Bob is in) — "
                "run /diagnostics there to check it.",
            ))
        else:
            lines.append(_channel_line(
                "Audit log", log_channel, me, ["view_channel", "send_messages", "embed_links"],
                "post merit audit logs",
            ))
            lines.append(
                (True, "Audit log: can @everyone (used to flag Bonus awards over 3).")
                if log_channel.permissions_for(me).mention_everyone
                else (False, "Audit log: missing **Mention Everyone** → the @everyone alert for large Bonus awards won't actually ping anyone.")
            )

    # ── Voice channels ────────────────────────────────────────────────────────
    voice_perms = ["view_channel", "connect", "speak"]
    voice_channels = [*guild.voice_channels, *guild.stage_channels]
    if not voice_channels:
        lines.append((None, "Voice: this server has no voice channels."))
    else:
        blocked = [c for c in voice_channels if _missing(c.permissions_for(me), voice_perms)]
        if not blocked:
            lines.append((True, f"Voice: can join and speak in all {len(voice_channels)} voice channels."))
        else:
            shown = ", ".join(
                f"<#{c.id}> ({_human(_missing(c.permissions_for(me), voice_perms))})" for c in blocked[:8]
            )
            more = f" and {len(blocked) - 8} more" if len(blocked) > 8 else ""
            lines.append((
                None if len(blocked) < len(voice_channels) else False,
                f"Voice: can join and speak in {len(voice_channels) - len(blocked)}/{len(voice_channels)} "
                f"voice channels. Blocked: {shown}{more}.",
            ))
    lines.append(
        (True, "Voice-state intent: enabled.")
        if intents.voice_states
        else (False, "Voice-state intent: not enabled — greetings and auto-leave won't work.")
    )
    lines.append(
        (True, "Members intent: enabled (needed to look members up by name).")
        if intents.members
        else (False, "Members intent: not enabled → turn on Server Members Intent in the Developer Portal.")
    )

    # ── Rank roles: exist, and sit below Bob so he could manage them ──────────
    for name in (config.HR_ROLE_NAME, config.ADVISOR_ROLE_NAME, config.ROYALTY_ROLE_NAME):
        role = discord.utils.get(guild.roles, name=name)
        if not role:
            lines.append((None, f'Role "{name}": doesn\'t exist in this server.'))
        elif me.top_role > role:
            lines.append((True, f'Role "{name}": exists and sits below Bob\'s role (manageable).'))
        else:
            lines.append((
                None,
                f'Role "{name}": exists but is above Bob\'s highest role → drag Bob\'s role above it '
                "if Bob should ever assign it.",
            ))

    # ── Who holds the top ranks ───────────────────────────────────────────────
    lines.append(
        (True, f"Owners configured: {len(config.OWNER_USER_IDS)}.")
        if config.OWNER_USER_IDS
        else (False, "DISCORD_OWNER_USER_IDS is empty → nobody has owner rank, and only the Fire Lord and access list can talk to Bob.")
    )
    return lines


# ─── Voice runtime ────────────────────────────────────────────────────────────


def run_voice_runtime_checks() -> list[CheckLine]:
    """Can this host do voice at all?"""
    lines: list[CheckLine] = []

    try:
        import nacl
        lines.append((True, f"Voice encryption: PyNaCl {nacl.__version__}."))
    except ImportError:
        lines.append((False, "PyNaCl is not installed → Discord will refuse the voice connection. Run pip install -r requirements.txt."))

    try:
        if not discord.opus.is_loaded():
            discord.opus._load_default()
        lines.append(
            (True, "Audio encoder: Opus loaded.")
            if discord.opus.is_loaded()
            else (False, "No Opus audio encoder found → install libopus on the host.")
        )
    except Exception as e:
        lines.append((False, f"Couldn't load the Opus audio encoder: {e}"))

    # Discord requires DAVE end-to-end encryption for voice; discord.py only
    # speaks it from 2.7 on, with the davey package installed.
    version = tuple(int(p) for p in discord.__version__.split(".")[:2] if p.isdigit())
    try:
        import davey  # noqa: F401
        has_davey = True
    except ImportError:
        has_davey = False
    if version >= (2, 7) and has_davey:
        lines.append((True, f"DAVE end-to-end encryption: discord.py {discord.__version__} with davey."))
    else:
        lines.append((
            None,
            f"discord.py {discord.__version__}{'' if has_davey else ', davey not installed'} — Discord's voice "
            "encryption (DAVE) needs discord.py 2.7+ with the davey package. If Bob can't connect to "
            "voice, upgrade with `pip install -U \"discord.py[voice]\"`.",
        ))
    return lines


def voice_activity_lines(guild: discord.Guild, voice_manager) -> list[CheckLine]:
    """Where Bob is right now and the last few voice events on this server."""
    channel = voice_manager.current_channel(guild)
    if channel:
        pending = " (channel empty — auto-leave pending)" if voice_manager.alone_timer_running(guild.id) else ""
        lines: list[CheckLine] = [(True, f"In <#{channel.id}>, connected{pending}.")]
    else:
        lines = [(None, "Not in a voice channel right now.")]
    lines += [(None, f"<t:{at}:T> {text}") for at, text in voice_manager.recent_events(guild.id, 8)]
    return lines


# ─── Database ─────────────────────────────────────────────────────────────────


async def run_database_check() -> list[CheckLine]:
    if not config.DATABASE_URL:
        return [(False, "DATABASE_URL is not set → every merit command will fail until it is.")]
    started = time.monotonic()
    try:
        count = await merit.count_entries()
    except Exception as e:
        return [(False, f"Database unreachable: {str(e)[:150]} → check DATABASE_URL.")]
    took = int((time.monotonic() - started) * 1000)
    return [(True, f"Database reachable ({took} ms) — {count} merit ledger entries.")]


# ─── Report formatting ────────────────────────────────────────────────────────


def _icon(ok: bool | None) -> str:
    return "✅" if ok is True else "❌" if ok is False else "ℹ️"


def to_fields(title: str, lines: list[CheckLine]) -> list[tuple[str, str]]:
    """
    Packs check lines into embed fields (1024-char limit each), splitting a
    long section across "(cont.)" fields rather than truncating it.
    """
    fields: list[tuple[str, str]] = []
    current = ""
    for ok, text in lines:
        entry = f"{_icon(ok)} {text}"[:1000]
        if current and len(current) + 1 + len(entry) > 1024:
            fields.append((f"{title} (cont.)" if fields else title, current))
            current = entry
        else:
            current = f"{current}\n{entry}" if current else entry
    if current:
        fields.append((f"{title} (cont.)" if fields else title, current))
    return fields


EMBED_BUDGET = 5500  # under Discord's 6000, leaving room for title/footer
MAX_FIELDS = 25


def build_report_embeds(summary: str, color: int, fields: list[tuple[str, str]]) -> list[discord.Embed]:
    """Splits report fields across as many embeds as the size limits require."""
    groups: list[list[tuple[str, str]]] = [[]]
    size = len(summary)
    for name, value in fields:
        field_size = len(name) + len(value)
        if groups[-1] and (size + field_size > EMBED_BUDGET or len(groups[-1]) >= MAX_FIELDS):
            groups.append([(name, value)])
            size = field_size
        else:
            groups[-1].append((name, value))
            size += field_size

    embeds = []
    for i, group in enumerate(groups):
        embed = discord.Embed(
            title="FIRE NATION // SYSTEMS DIAGNOSTIC" + (" (cont.)" if i else ""),
            color=color,
        )
        if i == 0:
            embed.description = summary
        for name, value in group:
            embed.add_field(name=name, value=value, inline=False)
        if i == len(groups) - 1:
            embed.set_footer(text="✅ working • ❌ needs fixing • ℹ️ info / not tested")
            embed.timestamp = datetime.now(timezone.utc)
        embeds.append(embed)
    return embeds


async def build_report(
    guild: discord.Guild, current_channel, voice_manager, test_tts: bool, voice: str | None
) -> tuple[list[discord.Embed], bytes | None]:
    async def section(name: str, run) -> list[CheckLine]:
        try:
            result = run()
            return await result if hasattr(result, "__await__") else result
        except Exception as e:
            logger.error(f"Diagnostics section '{name}' crashed: {e}", exc_info=True)
            return [(False, f"This check crashed: {e}")]

    ai_lines = await section("ai", run_ai_checks)
    try:
        google_lines, wav = await run_google_checks(test_tts, voice)
    except Exception as e:
        logger.error(f"Diagnostics section 'google' crashed: {e}", exc_info=True)
        google_lines, wav = [(False, f"This check crashed: {e}")], None
    discord_lines = await section("discord", lambda: run_discord_checks(guild, current_channel, voice_manager.bot.intents))
    voice_lines = await section("voice", run_voice_runtime_checks)
    db_lines = await section("database", run_database_check)

    everything = [*ai_lines, *google_lines, *discord_lines, *voice_lines, *db_lines]
    failures = sum(1 for ok, _ in everything if ok is False)
    summary = (
        "All systems nominal."
        if failures == 0
        else f"{failures} issue{'' if failures == 1 else 's'} need{'s' if failures == 1 else ''} attention — "
             "fixes are listed beside each ❌."
    )
    embeds = build_report_embeds(
        summary,
        config.FIRE_ORANGE if failures == 0 else config.FIRE_RED,
        [
            *to_fields("AI provider", ai_lines),
            *to_fields("Google text-to-speech", google_lines),
            *to_fields("Discord permissions", discord_lines),
            *to_fields("Voice runtime", voice_lines),
            *to_fields("Voice activity", voice_activity_lines(guild, voice_manager)),
            *to_fields("Database", db_lines),
        ],
    )
    return embeds, wav
