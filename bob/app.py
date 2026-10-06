"""
The Discord client: startup, and deciding which messages Bob answers.

Bob responds — only to the Owner, the Fire Lord and the standing-access
list (see features/fire_nation/ranks.py) — to:
  - @mentions in any channel
  - Messages containing "bob" (case-insensitive)
  - DMs

No hardcoded channel IDs. Bot works server-wide.
"""

import logging
import re
from logging.handlers import RotatingFileHandler
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
    handlers=[
        logging.StreamHandler(),
        # The host has 512 MB of disk: keep at most 3 MB of log.
        RotatingFileHandler("bot.log", maxBytes=1_000_000, backupCount=2, encoding="utf-8"),
    ]
)
# One line per AI request adds nothing the provider's own logging doesn't say.
for noisy in ("httpx", "httpx2", "httpcore"):
    logging.getLogger(noisy).setLevel(logging.WARNING)
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
        from bob.ai import create_provider
        from bob.conversation.manager import ConversationManager
        from bob.voice.manager import get_voice_manager

        ai = create_provider()
        _voice_manager = get_voice_manager(bot)
        _manager = ConversationManager(ai, bot, voice_manager=_voice_manager)
        logger.info(f"ConversationManager initialized (provider={config.AI_PROVIDER}, model={config.get_model()})")
    return _manager


# ── Setup hook — runs before the bot connects ─────────────────────────────────
async def setup_hook():
    # Initialize DB tables
    from bob.conversation.memory import ensure_memory_tables
    await ensure_memory_tables()

    # Load Roblox monitor extension
    try:
        await bot.load_extension("bob.features.roblox_monitor")
        logger.info("Roblox monitor loaded")
    except Exception as e:
        logger.error(f"Failed to load roblox_monitor: {e}")

    # Merits, ranks, knowledge base, standing orders, voice lines, diagnostics
    try:
        await bot.load_extension("bob.features.fire_nation")
        logger.info("Fire Nation features loaded")
    except Exception as e:
        logger.error(f"Failed to load fire_nation: {e}", exc_info=True)

    # Sync slash commands. A failure here must not stop the bot from starting.
    try:
        if config.TEST_GUILD_ID:
            # Guild-scoped commands propagate almost instantly — use this while
            # testing so you don't have to wait up to an hour.
            guild = discord.Object(id=config.TEST_GUILD_ID)
            bot.tree.copy_global_to(guild=guild)
            synced = await bot.tree.sync(guild=guild)
        else:
            synced = await bot.tree.sync()
        logger.info(f"Slash commands synced: {[c.name for c in synced]}")
    except discord.HTTPException as e:
        logger.error(
            f"Failed to sync slash commands: {e}. Check that the bot was invited with the "
            "'applications.commands' scope and, if DISCORD_TEST_GUILD_ID is set, that the bot is in that guild."
        )


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
    from bob.conversation import memory as mem
    if mem.is_conversation_active(message.channel.id, message.author.id):
        return True
    # Name mention (case-insensitive, word boundary)
    content_lower = message.content.lower()
    if re.search(r"\bbob\b", content_lower):
        return True
    return False


# Just his name, with or without a greeting: "bob", "hey bob?", "yo bob!".
_NAME_ONLY = re.compile(r"^\W*(?:(?:hey|hi|yo|hello|ok|okay|oi|ay)\W+)?bob\W*$", re.IGNORECASE)


def _clean_content(message: discord.Message) -> str:
    """Remove @mention and 'hey bob' preamble from message content."""
    content = message.content
    if bot.user:
        content = content.replace(f"<@{bot.user.id}>", "").strip()
        content = content.replace(f"<@!{bot.user.id}>", "").strip()

    # Calling his name is not a request. Answered directly instead of being
    # sent to the AI, which is slow and, with nothing to go on, picks a tool
    # to run on its own.
    if _NAME_ONLY.match(content) and not message.attachments:
        return ""

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


_DISMISSAL = re.compile(
    r"^((ok(ay)?|please|bob)[, ]+)*(stop|quit)\b"
    r"|\b(shut up|be quiet|go away|leave me alone|end (the )?conversation|never ?mind)\b"
    r"|\bthank(s| you)\b|\bthat'?(s|ll be) all\b|\bthat'?s enough\b|\bgood ?bye\b|\bbye\b"
    r"|\bdismiss(ed)?\b|\byou'?re (free|dismissed)\b|\ball good\b",
    re.IGNORECASE,
)


def _is_dismissal(content: str) -> bool:
    """
    True for a short message that only ends the conversation ("thanks",
    "stop responding to me", "that'll be all"). Longer messages are left to
    the AI, so "thanks — now check the leaderboard" still gets answered.
    """
    words = content.split()
    return 0 < len(words) <= 6 and bool(_DISMISSAL.search(content))


# ── Events ────────────────────────────────────────────────────────────────────

@bot.event
async def on_ready():
    print(f"🔥 {bot.user.name} is online")
    print(f"   Provider: {config.AI_PROVIDER} | Model: {config.get_model()}")
    print(f"   Guilds: {len(bot.guilds)}")
    logger.info(f"Bot ready. Provider={config.AI_PROVIDER}, model={config.get_model()}")


@bot.command(name="dashboard")
async def dashboard_cmd(ctx):
    """Sends the live Roblox dashboard instantly, bypassing the AI."""
    from bob.features.roblox_monitor.dashboard import build_dashboard_embed
    from bob.features.roblox_monitor.db import DB_PATH
    import aiosqlite
    
    embed = await build_dashboard_embed()
    msg = await ctx.send(embed=embed)
    
    # Save to db so monitor.py can auto-update it
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT OR REPLACE INTO bot_status (key, value) VALUES ('live_dash_channel', ?)", (str(msg.channel.id),))
        await db.execute("INSERT OR REPLACE INTO bot_status (key, value) VALUES ('live_dash_msg', ?)", (str(msg.id),))
        await db.commit()


@bot.event
async def on_message(message: discord.Message):
    # Never respond to bots (including ourselves)
    if message.author.bot:
        return

    content = _clean_content(message)

    # Log every message to short-term channel memory
    from bob.conversation import memory as mem
    mem.add_to_history(
        message.channel.id,
        "user",
        content or message.content,
        message.author.display_name,
        message.author.id,
    )

    # "bob go to sleep" / "bob wake up" — while asleep, chat is ignored entirely.
    from bob.features.fire_nation import chat_hooks
    sleep_state = await chat_hooks.handle_sleep_wake(message, bot)
    if sleep_state == "handled":
        return

    # Bob only converses with the Owner, the Fire Lord and anyone on the
    # standing-access list. Everyone else still has the slash commands.
    from bob.features.fire_nation.ranks import has_access
    if (
        sleep_state == "asleep"
        or not has_access(message.author)
        or not _is_addressed_to_bob(message)
    ):
        await bot.process_commands(message)
        return

    from bob.conversation import memory as mem
    mem.activate_conversation(message.channel.id, message.author.id)

    # "join vc" / "leave vc" / "say that out loud" / "voice status"
    from bob.voice.manager import get_voice_manager
    if await chat_hooks.handle_voice_phrase(message, content, get_voice_manager(bot)):
        return

    # A natural way to end a conversation without needing a command.
    if _is_dismissal(content):
        mem.end_conversation(message.channel.id)
        await message.reply("Got it — I’ll stay quiet until you call me again.")
        return

    if not content:
        await message.reply("Yeah? What's up?")
        return

    manager = get_manager()
    await manager.handle(message, content)


# ── Run ───────────────────────────────────────────────────────────────────────
def run() -> None:
    bot.run(config.DISCORD_TOKEN, log_handler=None)
