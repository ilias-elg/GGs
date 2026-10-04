"""
Central configuration — reads all environment variables in one place.
Never import secrets from here into AI context.
"""
import os
from dotenv import load_dotenv

load_dotenv()

# ─── Discord ──────────────────────────────────────────────────────────────────
DISCORD_TOKEN: str = os.getenv("DISCORD_TOKEN", "")

# ─── AI Provider ──────────────────────────────────────────────────────────────
# AI_PROVIDER: groq | openai | anthropic | gemini
AI_PROVIDER: str = os.getenv("AI_PROVIDER", "groq").lower()
# AI_MODEL: leave empty to use the provider default
AI_MODEL: str = os.getenv("AI_MODEL", "")

GROQ_API_KEY: str = os.getenv("GROQ_API_KEY", "")
OPENAI_API_KEY: str = os.getenv("OPENAI_API_KEY", "")
ANTHROPIC_API_KEY: str = os.getenv("ANTHROPIC_API_KEY", "")

# ─── Conversation ─────────────────────────────────────────────────────────────
MAX_CONTEXT_MESSAGES: int = int(os.getenv("MAX_CONTEXT_MESSAGES", "5"))
MAX_TOOL_ROUNDS: int = int(os.getenv("MAX_TOOL_ROUNDS", "5"))
MEMORY_ENABLED: bool = os.getenv("MEMORY_ENABLED", "true").lower() == "true"
# After Bob is addressed, keep the conversation open for natural follow-ups
# from the same person. This avoids requiring "Bob" in every message while
# preventing the bot from replying to every message in a busy channel.
AUTO_FOLLOW_UPS: bool = os.getenv("AUTO_FOLLOW_UPS", "true").lower() == "true"
CONVERSATION_TTL_SECONDS: int = int(os.getenv("CONVERSATION_TTL_SECONDS", "1200"))
# Expose every Discord tool in guild conversations so natural language such as
# "delete the raid VC" cannot be blocked by a brittle keyword classifier.
# Tool calls are still permission-checked and mutation calls are confirmed.
FULL_DISCORD_TOOLS: bool = os.getenv("FULL_DISCORD_TOOLS", "false").lower() == "true"
# When true, Bob receives every tool that is available for the current
# conversation instead of relying on keyword intent detection.
FULL_TOOLSET: bool = os.getenv("FULL_TOOLSET", "false").lower() == "true"

# ─── General outside-Discord tasks ───────────────────────────────────────────
# Disabled by default. When enabled, only the listed Discord user IDs may ask
# Bob to run a single, non-shell command inside the configured workspace.
LOCAL_TASKS_ENABLED: bool = os.getenv("LOCAL_TASKS_ENABLED", "false").lower() == "true"
LOCAL_TASK_WORKSPACE: str = os.getenv(
    "LOCAL_TASK_WORKSPACE",
    os.path.dirname(os.path.abspath(__file__)),
)
LOCAL_TASK_USER_IDS: frozenset[int] = frozenset(
    int(value.strip())
    for value in os.getenv("LOCAL_TASK_USER_IDS", "").split(",")
    if value.strip().isdigit()
)
LOCAL_TASK_TIMEOUT_SECONDS: int = int(os.getenv("LOCAL_TASK_TIMEOUT_SECONDS", "30"))

# ─── Web Search ───────────────────────────────────────────────────────────────
WEB_SEARCH_ENABLED: bool = os.getenv("WEB_SEARCH_ENABLED", "true").lower() == "true"
# Optional: set to "brave" or "serpapi" with WEB_SEARCH_API_KEY for better results
SEARCH_ENGINE: str = os.getenv("SEARCH_ENGINE", "duckduckgo").lower()
WEB_SEARCH_API_KEY: str = os.getenv("WEB_SEARCH_API_KEY", "")

# ─── Voice ────────────────────────────────────────────────────────────────────
VOICE_JOIN_SOUND_ENABLED: bool = os.getenv("VOICE_JOIN_SOUND_ENABLED", "false").lower() == "true"
VOICE_JOIN_SOUND_PATH: str = os.getenv("VOICE_JOIN_SOUND_PATH", "")
TTS_ENABLED: bool = os.getenv("TTS_ENABLED", "false").lower() == "true"

# ─── Fire Nation ranks, merits, knowledge, voice (ported from Jarvis) ────────
# Env names match Jarvis's where they mean the same thing, so its values can
# be copied straight across.

def _first_env(*names: str) -> str:
    for name in names:
        value = os.getenv(name, "").strip()
        if value:
            return value
    return ""


def _id_set(*names: str) -> frozenset[int]:
    return frozenset(
        int(value.strip())
        for value in _first_env(*names).split(",")
        if value.strip().isdigit()
    )


HR_ROLE_NAME = "HR"
ADVISOR_ROLE_NAME = "Advisor"
ROYALTY_ROLE_NAME = "Royalty"

FIRE_RED = 0xB91C1C
FIRE_ORANGE = 0xF97316

OWNER_USER_IDS: frozenset[int] = _id_set("DISCORD_OWNER_USER_IDS")
SECOND_IN_COMMAND_USER_IDS: frozenset[int] = _id_set("DISCORD_SECOND_IN_COMMAND_USER_IDS")
HR_ROLE_IDS: frozenset[int] = _id_set("DISCORD_HR_ROLE_IDS")
# Every merit action is audit-logged to this channel.
OWNER_LOG_CHANNEL_ID: int = int(_first_env("DISCORD_OWNER_LOG_CHANNEL_ID") or 0)
# Slash commands registered to one guild appear instantly; global ones can
# take up to an hour.
TEST_GUILD_ID: int = int(_first_env("DISCORD_TEST_GUILD_ID") or 0)

# Postgres connection string for the merit ledger (Neon, Supabase, Railway…).
# Pointing this at Jarvis's database carries its merit data over as-is.
DATABASE_URL: str = _first_env("DATABASE_URL")

# Runtime state (access list, standing orders, greetings, added knowledge,
# cached voice clips). Must survive restarts and redeploys.
DATA_DIR: str = _first_env("BOT_DATA_DIR", "JARVIS_DATA_DIR") or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "data"
)

# Google AI Studio key. Spoken voice lines use Gemini text-to-speech (without
# a key Bob can still join voice, he just can't talk), and AI_PROVIDER=gemini
# uses the same key for chat.
GOOGLE_API_KEY: str = _first_env("GOOGLE_API_KEY")
TTS_MODEL: str = _first_env("TTS_MODEL", "JARVIS_TTS_MODEL")
TTS_VOICE: str = _first_env("TTS_VOICE", "JARVIS_TTS_VOICE") or "Algenib"
TTS_DELIVERY: str = _first_env("TTS_DELIVERY") or (
    "Speak in a low, calm, dry and confident male voice. Read this line:"
)
# Ordinary text channels to treat as announcement channels (read aloud in voice).
ANNOUNCEMENT_CHANNEL_IDS: frozenset[int] = _id_set(
    "ANNOUNCEMENT_CHANNEL_IDS", "JARVIS_ANNOUNCEMENT_CHANNEL_IDS"
)
# Comma-separated direct image URLs to cycle the avatar through every 4 hours.
AVATAR_URLS: list[str] = [
    url.strip()
    for url in _first_env("AVATAR_URLS", "JARVIS_AVATAR_URLS").split(",")
    if url.strip()
]

# ─── Roblox Monitor (passed through to keep roblox_monitor self-contained) ───
ALERTS_CHANNEL_ID: str = os.getenv("ALERTS_CHANNEL_ID", "")
ALERT_USER_ID: str = os.getenv("ALERT_USER_ID", "")

# ─── Default models per provider ──────────────────────────────────────────────
_PROVIDER_DEFAULTS: dict[str, str] = {
    "groq": "groq/compound",
    "openai": "gpt-4o-mini",
    "anthropic": "claude-3-5-haiku-20241022",
    "gemini": "gemini-3.8-flash",
}


def get_model() -> str:
    """Return the configured model, falling back to the provider's recommended default."""
    if AI_PROVIDER == "gemini" and not AI_MODEL:
        # Jarvis's variable name, so its settings carry over unchanged.
        return _first_env("GEMINI_MODEL") or _PROVIDER_DEFAULTS["gemini"]
    return AI_MODEL or _PROVIDER_DEFAULTS.get(AI_PROVIDER, "groq/compound")


# Fast/cheap model used for background tasks (memory extraction, summarization).
# Keeps the main quota free for real conversations.
AI_BACKGROUND_MODEL: str = os.getenv("AI_BACKGROUND_MODEL", "")

_BACKGROUND_DEFAULTS: dict[str, str] = {
    "groq": "groq/compound-mini",   # smaller/faster model for cheap background calls
    "openai": "gpt-4o-mini",
    "anthropic": "claude-3-haiku-20240307",
}
# Gemini has no cheaper background default: it reuses the chat model.


def get_background_model() -> str:
    """Return the lightweight model for background AI tasks."""
    return AI_BACKGROUND_MODEL or _BACKGROUND_DEFAULTS.get(AI_PROVIDER, get_model())


def validate() -> list[str]:
    """Return a list of configuration error messages. Empty = all good."""
    errors: list[str] = []

    if not DISCORD_TOKEN:
        errors.append("DISCORD_TOKEN is missing from .env")

    if AI_PROVIDER == "groq" and not GROQ_API_KEY:
        errors.append("GROQ_API_KEY is missing (required when AI_PROVIDER=groq)")
    elif AI_PROVIDER == "openai" and not OPENAI_API_KEY:
        errors.append("OPENAI_API_KEY is missing (required when AI_PROVIDER=openai)")
    elif AI_PROVIDER == "gemini" and not GOOGLE_API_KEY:
        errors.append("GOOGLE_API_KEY is missing (required when AI_PROVIDER=gemini)")
    elif AI_PROVIDER == "anthropic" and not ANTHROPIC_API_KEY:
        errors.append("ANTHROPIC_API_KEY is missing (required when AI_PROVIDER=anthropic)")
    elif AI_PROVIDER not in _PROVIDER_DEFAULTS:
        errors.append(
            f"Unknown AI_PROVIDER '{AI_PROVIDER}'. Valid options: groq, openai, anthropic, gemini"
        )

    return errors
