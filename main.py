import os
import discord
from discord.ext import commands
from openai import OpenAI
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

# We need these two tokens to run the bot
DISCORD_TOKEN = os.getenv('DISCORD_TOKEN')
GROQ_API_KEY = os.getenv('GROQ_API_KEY')

if not DISCORD_TOKEN or not GROQ_API_KEY:
    print("Error: Missing DISCORD_TOKEN or GROQ_API_KEY in the .env file.")
    exit(1)

# Initialize the OpenAI client pointing to Groq's super-fast API
ai_client = OpenAI(
    base_url="https://api.groq.com/openai/v1",
    api_key=GROQ_API_KEY
)

# Set up Discord bot intents (Message Content is required to read what users say)
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

async def setup_hook():
    await bot.load_extension('roblox_monitor')
    await bot.tree.sync()

bot.setup_hook = setup_hook

# The Groq-hosted model we'll use
MODEL_NAME = "openai/gpt-oss-120b"

# Keywords that signal the user wants the live dashboard
DASHBOARD_TRIGGERS = [
    "show me the dashboard",
    "show dashboard",
    "open dashboard",
    "dashboard",
    "show stats",
    "show me stats",
    "group stats",
    "roblox stats",
    "who's playing",
    "whos playing",
    "monitor status",
]

def wants_dashboard(text: str) -> bool:
    """Check if the message is asking for the live dashboard."""
    text = text.lower()
    return any(trigger in text for trigger in DASHBOARD_TRIGGERS)

def is_addressed_to_bob(message: discord.Message) -> bool:
    """Returns True if the message is directed at Bob."""
    content = message.content.lower()
    is_mentioned = message.guild and message.mentions and any(
        u.id == message.guild.me.id for u in message.mentions
    ) if message.guild else bot.user in message.mentions
    is_named = "bob" in content
    is_command = message.content.startswith("!ask")
    return is_mentioned or is_named or is_command

@bot.event
async def on_ready():
    print(f'🔥 Logged in successfully as {bot.user.name}')
    print(f'Ready to answer questions in your server!')

@bot.event
async def on_message(message):
    # Don't let the bot reply to itself
    if message.author == bot.user:
        return

    if not is_addressed_to_bob(message):
        await bot.process_commands(message)
        return

    # Clean the message text
    content = message.content
    content = content.replace(f'<@{bot.user.id}>', '').replace('!ask', '').strip()
    # Remove "bob" and "hey" from the start so the AI gets the actual intent
    clean = content.lower().lstrip()
    for prefix in ["hey bob,", "hey bob", "bob,"]:
        if clean.startswith(prefix):
            content = content[len(prefix):].strip()
            break

    if not content:
        await message.channel.send("Hey! What's up? Ask me anything or say **\"show me the dashboard\"** to see live group stats!")
        return

    # --- DASHBOARD REQUEST ---
    if wants_dashboard(content):
        async with message.channel.typing():
            try:
                # Import the dashboard builder from roblox_monitor
                from roblox_monitor.dashboard import build_dashboard_embed
                embed = await build_dashboard_embed()
                await message.reply(embed=embed)
            except Exception as e:
                await message.reply(f"❌ Couldn't load the dashboard right now: {e}")
        return

    # --- AI RESPONSE ---
    async with message.channel.typing():
        try:
            completion = ai_client.chat.completions.create(
                model=MODEL_NAME,
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "You are Bob, a witty and intelligent Discord bot for a Roblox group monitoring server. "
                            "You monitor TSB Air, TSB Earth, and TSB Water Roblox groups for player activity and spikes. "
                            "Keep responses concise and well formatted for Discord. "
                            "If someone asks about live stats or the dashboard, tell them to say 'show me the dashboard'."
                        )
                    },
                    {
                        "role": "user",
                        "content": content
                    }
                ],
                temperature=0.7,
                max_tokens=1024
            )

            answer = completion.choices[0].message.content

            # Token usage bar
            tokens_used = completion.usage.total_tokens
            limit = 8000
            percent = min(tokens_used / limit, 1.0)
            bar_length = 20
            filled = int(bar_length * percent)
            bar = '█' * filled + '░' * (bar_length - filled)
            usage_text = f"\n\n`Tokens: {tokens_used} / {limit} [{bar}]`"
            answer += usage_text

            if len(answer) > 2000:
                answer = answer[:1996] + "..."

            await message.reply(answer)

        except Exception as e:
            await message.reply(f"❌ An error occurred: {e}")

    await bot.process_commands(message)

# Start the bot
bot.run(DISCORD_TOKEN)
