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

# The Groq-hosted model we'll use (GPT OSS 120B is the most capable model available)
MODEL_NAME = "openai/gpt-oss-120b" 

@bot.event
async def on_ready():
    print(f'🔥 Logged in successfully as {bot.user.name}')
    print(f'Ready to answer questions in your server!')

@bot.event
async def on_message(message):
    # Don't let the bot reply to itself
    if message.author == bot.user:
        return

    # Check if the bot is mentioned OR if the message starts with !ask
    is_mentioned = bot.user in message.mentions
    is_command = message.content.startswith("!ask")

    if is_mentioned or is_command:
        # Clean up the message text so the bot doesn't see its own ID or the command prefix
        question = message.content.replace(f'<@{bot.user.id}>', '').replace('!ask', '').strip()
        
        if not question:
            await message.channel.send("Did you need something? Ask me a question!")
            return

        # Shows the "bot is typing..." indicator in Discord
        async with message.channel.typing():
            try:
                # Send the request to Groq API
                completion = ai_client.chat.completions.create(
                    model=MODEL_NAME,
                    messages=[
                        {
                            "role": "system", 
                            "content": "You are a helpful, witty, and smart Discord bot. Keep responses concise and formatted well for Discord."
                        },
                        {
                            "role": "user", 
                            "content": question
                        }
                    ],
                    temperature=0.7,
                    max_tokens=1024
                )
                
                # Extract the AI's response text
                answer = completion.choices[0].message.content
                
                # Calculate token usage for the bar (limit is 8K tokens per minute for this model)
                tokens_used = completion.usage.total_tokens
                limit = 8000
                percent = min(tokens_used / limit, 1.0)
                bar_length = 20
                filled = int(bar_length * percent)
                bar = '█' * filled + '░' * (bar_length - filled)
                
                usage_text = f"\n\n`Tokens used (this request): {tokens_used} / {limit} [{bar}]`"
                
                # Append the usage text to the answer
                answer += usage_text
                
                # Discord has a 2000 character limit per message
                if len(answer) > 2000:
                    answer = answer[:1996] + "..."

                await message.reply(answer)

            except Exception as e:
                await message.reply(f"❌ An error occurred: {e}")
        
        # We handled it, so stop here
        return

    # Required so other commands can still run (if you add more later)
    await bot.process_commands(message)

# Start the bot
bot.run(DISCORD_TOKEN)
