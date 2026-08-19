import os
import discord
from discord.ext import commands
from openai import OpenAI
from dotenv import load_dotenv

load_dotenv()

DISCORD_TOKEN = os.getenv('DISCORD_TOKEN')
GROQ_API_KEY = os.getenv('GROQ_API_KEY')

if not DISCORD_TOKEN or not GROQ_API_KEY:
    print("Error: Missing DISCORD_TOKEN or GROQ_API_KEY in the .env file.")
    exit(1)

# Groq via OpenAI-compatible client
ai_client = OpenAI(
    base_url="https://api.groq.com/openai/v1",
    api_key=GROQ_API_KEY,
)

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

# The one channel where Bob responds to EVERYTHING (no need to say his name)
AI_CHANNEL_ID = 1090066231312261133

# Lazy-loaded on first message so the event loop is running
_manager = None

def get_manager():
    global _manager
    if _manager is None:
        from conversation.manager import ConversationManager
        _manager = ConversationManager(ai_client)
    return _manager


async def setup_hook():
    await bot.load_extension('roblox_monitor')
    await bot.tree.sync()

bot.setup_hook = setup_hook


def _is_addressed_to_bob(message: discord.Message) -> bool:
    if message.channel.id == AI_CHANNEL_ID:
        return True
    if bot.user and bot.user in message.mentions:
        return True
    if "bob" in message.content.lower():
        return True
    return False


def _clean_content(message: discord.Message) -> str:
    """Strip mention and 'hey bob' preamble from message content."""
    content = message.content
    if bot.user:
        content = content.replace(f"<@{bot.user.id}>", "").strip()
    lower = content.lower()
    for prefix in ("hey bob,", "hey bob"):
        if lower.startswith(prefix):
            content = content[len(prefix):].strip()
            break
    return content.strip()


DASHBOARD_TRIGGERS = {
    "show me the dashboard", "show dashboard", "open dashboard",
    "dashboard", "show stats", "who's playing", "whos playing",
}

MEMORY_QUERIES = {
    "what do you remember", "what do you know about me",
    "what have you saved", "show my memories",
}

FORGET_PREFIXES = ("forget ", "delete ", "remove ")
FORGET_EXACT = {"forget that", "forget it", "delete that", "remove that"}


@bot.event
async def on_ready():
    print(f'🔥 Logged in as {bot.user.name}')
    print(f'AI channel: {AI_CHANNEL_ID}')


@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    if not _is_addressed_to_bob(message):
        await bot.process_commands(message)
        return

    content = _clean_content(message)
    lower = content.lower().strip()
    manager = get_manager()

    if not content:
        if message.channel.id != AI_CHANNEL_ID:
            await message.reply("Yeah? What's up?")
        return

    # ── Memory: show all memories ────────────────────────────────────────────
    if any(q in lower for q in MEMORY_QUERIES):
        await manager.show_memories(message)
        return

    # ── Memory: forget something ─────────────────────────────────────────────
    fragment = ""
    is_forget = lower in FORGET_EXACT
    if not is_forget:
        for prefix in FORGET_PREFIXES:
            if lower.startswith(prefix):
                is_forget = True
                fragment = lower[len(prefix):].strip()
                break
    if is_forget:
        if not fragment:
            # "forget that" — find the last thing the user asked to remember
            from conversation import memory as mem
            for entry in reversed(mem.get_history(message.channel.id)):
                if entry.get("role") == "user" and "remember" in entry.get("content", "").lower():
                    fragment = entry["content"].lower().replace("remember", "").strip(" ,.")
                    break
        await manager.forget(message, fragment)
        return

    # ── Test DM shortcut ─────────────────────────────────────────────────────
    if "test dm" in lower:
        try:
            await message.author.send("Yo! DMs are working perfectly. If you set your ID in the `.env` file, I'll send spike alerts here.")
            await message.reply("Just sent you a DM. If you didn't get it, check your Privacy Settings (Allow direct messages from server members).")
        except discord.Forbidden:
            await message.reply("❌ I tried to DM you, but Discord blocked it. You need to enable **Allow direct messages from server members** in your Privacy Settings.")
        except Exception as e:
            await message.reply(f"❌ Couldn't DM you: {e}")
        return

    # ── Dashboard shortcut ───────────────────────────────────────────────────
    if any(t in lower for t in DASHBOARD_TRIGGERS):
        async with message.channel.typing():
            try:
                from roblox_monitor.dashboard import build_dashboard_embed
                embed = await build_dashboard_embed()
                await message.reply(embed=embed)
            except Exception as e:
                await message.reply(f"❌ Couldn't load the dashboard: {e}")
        return

    # ── Full conversational AI ───────────────────────────────────────────────
    await manager.handle(message, content)

# Start the bot
bot.run(DISCORD_TOKEN)
