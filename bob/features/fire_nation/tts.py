"""
Google Generative Language REST helpers + text-to-speech, and the text/audio
preparation voice playback needs. Shared by /diagnostics and the voice
feature so both make the exact same requests.
"""

import audioop
import base64
import hashlib
import logging
import os
import re
import struct
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx

import config

logger = logging.getLogger("discord")

API_BASE = "https://generativelanguage.googleapis.com/v1beta"
TIMEOUT_SECONDS = 20
VOICE_CACHE_DIR = os.path.join(config.DATA_DIR, "voice-cache")

# Gemini TTS prebuilt voices worth auditioning — Google's own one-word
# descriptions are in brackets. Try them with `/diagnostics tts:True voice:<name>`,
# then set TTS_VOICE to make one permanent.
VOICES = [
    ("Algenib", "Algenib (gravelly) — default"),
    ("Charon", "Charon (deep, informative)"),
    ("Orus", "Orus (firm)"),
    ("Alnilam", "Alnilam (firm)"),
    ("Schedar", "Schedar (even)"),
    ("Sadaltager", "Sadaltager (knowledgeable)"),
    ("Algieba", "Algieba (smooth)"),
    ("Iapetus", "Iapetus (clear)"),
    ("Enceladus", "Enceladus (breathy)"),
    ("Fenrir", "Fenrir (excitable)"),
]


def resolve_voice(requested: str | None = None) -> str:
    return (requested or "").strip() or config.TTS_VOICE


# ─── REST helpers ─────────────────────────────────────────────────────────────


async def google_fetch(path: str, api_key: str, body: dict | None = None) -> tuple[int, object]:
    async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS) as client:
        if body is None:
            res = await client.get(f"{API_BASE}/{path}", headers={"x-goog-api-key": api_key})
        else:
            res = await client.post(f"{API_BASE}/{path}", headers={"x-goog-api-key": api_key}, json=body)
    try:
        return res.status_code, res.json()
    except ValueError:
        return res.status_code, res.text


def _first(body: object) -> dict:
    first = body[0] if isinstance(body, list) and body else body
    return first if isinstance(first, dict) else {}


def google_error_message(body: object) -> str:
    """Google's own error sentence from either response shape (object or [object])."""
    err = _first(body).get("error") or {}
    if err.get("message"):
        return f"{err['status']}: {err['message']}" if err.get("status") else err["message"]
    return body[:200] if isinstance(body, str) and body else "no details"


def error_reason(body: object) -> str | None:
    details = (_first(body).get("error") or {}).get("details") or []
    return next((d["reason"] for d in details if isinstance(d, dict) and d.get("reason")), None)


async def list_models(api_key: str) -> tuple[int, object, list[dict]]:
    """Returns (status, error_body, models). status is 200 when the listing worked."""
    models: list[dict] = []
    page_token = ""
    for _ in range(10):
        path = "models?pageSize=1000" + (f"&pageToken={page_token}" if page_token else "")
        status, body = await google_fetch(path, api_key)
        if status != 200:
            return status, body, []
        models.extend(body.get("models", []))
        page_token = body.get("nextPageToken", "")
        if not page_token:
            break
    return 200, None, models


def short_name(model: dict) -> str:
    return model["name"].removeprefix("models/")


def pick_tts_model(models: list[dict]) -> str | None:
    """Picks the TTS model to use: a "flash" one if available (cheapest), else the first."""
    tts = [
        short_name(m)
        for m in models
        if re.search("tts", m["name"], re.IGNORECASE)
        and "generateContent" in m.get("supportedGenerationMethods", ["generateContent"])
    ]
    return next((n for n in tts if "flash" in n.lower()), tts[0] if tts else None)


def build_tts_request(text: str, voice: str) -> dict:
    """Request body for one spoken line. Gemini TTS follows plain-language style
    directions placed before the text to be spoken."""
    return {
        "contents": [{"parts": [{"text": f"{config.TTS_DELIVERY} {text}"}]}],
        "generationConfig": {
            "responseModalities": ["AUDIO"],
            "speechConfig": {"voiceConfig": {"prebuiltVoiceConfig": {"voiceName": voice}}},
        },
    }


def extract_audio(body: object) -> tuple[bytes, int] | None:
    """Pulls (pcm, sample_rate) out of a generateContent response, if it holds audio."""
    try:
        parts = body["candidates"][0]["content"]["parts"]
    except (KeyError, IndexError, TypeError):
        return None
    inline = next((p["inlineData"] for p in parts if p.get("inlineData", {}).get("data")), None)
    if not inline:
        return None
    rate = re.search(r"rate=(\d+)", inline.get("mimeType", ""))
    return base64.b64decode(inline["data"]), int(rate.group(1)) if rate else 24_000


def pcm_to_wav(pcm: bytes, sample_rate: int, channels: int = 1) -> bytes:
    """Wraps raw 16-bit little-endian PCM in a WAV header so Discord can play it."""
    header = struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF", 36 + len(pcm), b"WAVE", b"fmt ", 16, 1, channels,
        sample_rate, sample_rate * channels * 2, channels * 2, 16, b"data", len(pcm),
    )
    return header + pcm


# ─── Speech synthesis ─────────────────────────────────────────────────────────


@dataclass
class Speech:
    ok: bool
    pcm: bytes = b""
    sample_rate: int = 24_000
    cached: bool = False
    reason: str = ""
    quota_hit: bool = False


_discovered_tts_model: str | None = None


async def resolve_tts_model(api_key: str) -> str | None:
    """TTS_MODEL if set, otherwise the TTS model this key can use (looked up once)."""
    global _discovered_tts_model
    if config.TTS_MODEL:
        return config.TTS_MODEL
    if not _discovered_tts_model:
        status, _, models = await list_models(api_key)
        if status != 200:
            return None
        _discovered_tts_model = pick_tts_model(models)
    return _discovered_tts_model


def _cache_path(model: str, voice: str, text: str) -> str:
    key = hashlib.sha1(f"{model}|{voice}|{config.TTS_DELIVERY}|{text}".encode()).hexdigest()
    return os.path.join(VOICE_CACHE_DIR, f"{key}.pcm")


async def synthesize_speech(text: str, voice: str | None = None, cache: bool = False) -> Speech:
    """
    Turns text into raw 16-bit mono PCM. `cache` keeps a copy on disk — used
    for fixed lines ("Welcome back.") so each one only ever costs a single TTS
    request. Cached clips are always stored at 24 kHz, which is what Gemini
    TTS returns.
    """
    api_key = config.GOOGLE_API_KEY
    if not api_key:
        return Speech(False, reason="GOOGLE_API_KEY is not set, so I have no voice")
    voice = resolve_voice(voice)

    try:
        model = await resolve_tts_model(api_key)
    except httpx.HTTPError as e:
        return Speech(False, reason=f"couldn't reach Google ({e})")
    if not model:
        return Speech(False, reason="no text-to-speech model is available to this API key")

    path = _cache_path(model, voice, text) if cache else None
    if path:
        try:
            with open(path, "rb") as f:
                return Speech(True, pcm=f.read(), cached=True)
        except OSError:
            pass  # not cached yet

    try:
        status, body = await google_fetch(f"models/{model}:generateContent", api_key, build_tts_request(text, voice))
    except httpx.HTTPError as e:
        return Speech(False, reason=f"couldn't reach Google ({e})")
    if status != 200:
        return Speech(False, reason=google_error_message(body), quota_hit=status == 429)

    audio = extract_audio(body)
    if not audio:
        return Speech(False, reason="Google answered but sent no audio")
    pcm, sample_rate = audio

    if path and sample_rate == 24_000:
        try:
            os.makedirs(VOICE_CACHE_DIR, exist_ok=True)
            with open(path, "wb") as f:
                f.write(pcm)
        except OSError as e:
            logger.warning(f"Could not cache a voice clip — it will be regenerated next time: {e}")
    return Speech(True, pcm=pcm, sample_rate=sample_rate)


# ─── Audio + text preparation for voice playback ─────────────────────────────


def to_discord_pcm(pcm: bytes, sample_rate: int) -> bytes:
    """
    Converts 16-bit mono PCM at any sample rate into the 48 kHz 16-bit stereo
    PCM Discord voice expects. Doing it here avoids needing ffmpeg for speech.
    """
    if sample_rate != 48_000:
        pcm, _ = audioop.ratecv(pcm, 2, 1, sample_rate, 48_000, None)
    return audioop.tostereo(pcm, 2, 1, 1)


MAX_SPOKEN_CHARS = 600

_EMOJI = re.compile("[\U0001F000-\U0001FAFF☀-➿⬀-⯿️‍]")


def _spoken_timestamp(match: re.Match) -> str:
    try:
        when = datetime.fromtimestamp(int(match.group(1)), ZoneInfo("America/New_York"))
    except (ZoneInfoNotFoundError, ValueError, OverflowError, OSError):
        return ""
    return when.strftime("%A %H:%M")


def clean_for_speech(text: str, guild=None) -> str:
    """
    Turns Discord message text into something worth saying aloud: mentions
    become names, links/markdown/custom emoji are dropped, and very long text
    is cut at a sentence boundary (long clips are slow to generate and tedious
    to listen to).
    """

    def user(match: re.Match) -> str:
        member = guild.get_member(int(match.group(1))) if guild else None
        return member.display_name if member else "someone"

    def role(match: re.Match) -> str:
        found = guild.get_role(int(match.group(1))) if guild else None
        return found.name if found else "a role"

    def channel(match: re.Match) -> str:
        found = guild.get_channel(int(match.group(1))) if guild else None
        return found.name if found else "a channel"

    t = re.sub(r"<@!?(\d+)>", user, text)
    t = re.sub(r"<@&(\d+)>", role, t)
    t = re.sub(r"<#(\d+)>", channel, t)
    t = re.sub(r"@(everyone|here)\b", "everyone", t)
    t = re.sub(r"<a?:\w+:\d+>", "", t)  # custom emoji
    t = re.sub(r"<t:(\d+)(:[a-zA-Z])?>", _spoken_timestamp, t)
    t = re.sub(r"```[\s\S]*?```", "", t)
    t = re.sub(r"https?://\S+", "", t)
    t = re.sub(r"[*_~`|>#]+", "", t)
    t = _EMOJI.sub("", t)
    t = " ".join(t.split())

    if len(t) > MAX_SPOKEN_CHARS:
        cut = t[:MAX_SPOKEN_CHARS]
        last_stop = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "))
        t = (cut[: last_stop + 1] if last_stop > MAX_SPOKEN_CHARS / 2 else cut) + " The rest is in the channel."
    return t
