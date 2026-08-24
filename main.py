"""
Fire Nation Bot — main entry point.

Bob responds to:
  - @mentions in any channel
  - Messages containing "bob" (case-insensitive)
  - DMs

No hardcoded channel IDs. Bot works server-wide.
"""

import asyncio
import logging
import discord
from discord.ext import commands

import config

# ── Validate config before anything else ─────────────────────────────────────
errors = config.validate()
if errors:
    for err in errors:
        print(f"[CONFIG ERROR] {err}")
    raise SystemExit(1)

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("discord")

# ── Discord intents ───────────────────────────────────────────────────────────
intents = discord.Intents.default()
intents.message_content = True
intents.members = True  # required for member resolution in admin tools

bot = commands.Bot(command_prefix="!", intents=intents)

# ── Lazy-initialized components ───────────────────────────────────────────────
_manager = None
_voice_manager = None


def get_manager():
    global _manager, _voice_manager
    if _manager is None:
        from ai import create_provider
        from conversation.manager import ConversationManager
        from voice.manager import VoiceManager

        ai = create_provider()
        _voice_manager = VoiceManager(bot)
        _manager = ConversationManager(ai, bot, voice_manager=_voice_manager)
        logger.info(f"ConversationManager initialized (provider={config.AI_PROVIDER}, model={config.get_model()})")
    return _manager


# ── Setup hook — runs before the bot connects ─────────────────────────────────
async def setup_hook():
    # Initialize DB tables
    from conversation.memory import ensure_memory_tables
    await ensure_memory_tables()

    # Load Roblox monitor extension
    try:
        await bot.load_extension("roblox_monitor")
        logger.info("Roblox monitor loaded")
    except Exception as e:
        logger.error(f"Failed to load roblox_monitor: {e}")

    # Sync slash commands (none currently, but ready for future use)
    await bot.tree.sync()


bot.setup_hook = setup_hook


# ── Address detection ─────────────────────────────────────────────────────────

def _is_addressed_to_bob(message: discord.Message) -> bool:
    """Determine if Bob should respond to this message."""
    # Always respond to DMs
    if isinstance(message.channel, discord.DMChannel):
        return True
    # @mention
    if bot.user and bot.user in message.mentions:
        return True
    # Name mention (case-insensitive, word boundary)
    content_lower = message.content.lower()
    if "bob" in content_lower:
        return True
    return False


def _clean_content(message: discord.Message) -> str:
    """Remove @mention and 'hey bob' preamble from message content."""
    content = message.content
    if bot.user:
        content = content.replace(f"<@{bot.user.id}>", "").strip()
        content = content.replace(f"<@!{bot.user.id}>", "").strip()

    lower = content.lower()
    for prefix in ("hey bob,", "hey bob", "bob,"):
        if lower.startswith(prefix):
            content = content[len(prefix):].strip()
            break

    # Attach image URLs to content so the AI is aware of them
    if message.attachments:
        img_urls = [
            att.url for att in message.attachments
            if att.content_type and att.content_type.startswith("image/")
        ]
        if img_urls:
            content += "\n\n[Attached Images: " + ", ".join(img_urls) + "]"

    return content.strip()


# ── Events ────────────────────────────────────────────────────────────────────

@bot.event
async def on_ready():
    print(f"🔥 {bot.user.name} is online")
    print(f"   Provider: {config.AI_PROVIDER} | Model: {config.get_model()}")
    print(f"   Guilds: {len(bot.guilds)}")
    logger.info(f"Bot ready. Provider={config.AI_PROVIDER}, model={config.get_model()}")


@bot.event
async def on_message(message: discord.Message):
    # Never respond to bots (including ourselves)
    if message.author.bot:
        return

    content = _clean_content(message)

    # Log every message to short-term channel memory
    from conversation import memory as mem
    mem.add_to_history(
        message.channel.id,
        "user",
        content or message.content,
        message.author.display_name,
        message.author.id,
    )

    if not _is_addressed_to_bob(message):
        await bot.process_commands(message)
        return

    if not content:
        await message.reply("Yeah? What's up?")
        return

    manager = get_manager()
    await manager.handle(message, content)


# ── Run ───────────────────────────────────────────────────────────────────────
bot.run(config.DISCORD_TOKEN, log_handler=None)
