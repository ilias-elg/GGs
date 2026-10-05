"""
Bob, the Fire Nation Discord bot.

    app.py         the Discord client: startup, and deciding which messages Bob answers
    ai/            model providers (Gemini, Groq, OpenAI, Anthropic)
    conversation/  the chat pipeline: prompt building, tool loop, memory
    tools/         the functions the AI can call
    voice/         the voice connection and speech queue
    features/      self-contained features, each loaded as a discord.py extension
"""
