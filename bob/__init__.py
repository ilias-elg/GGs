"""
The Grand Archivist, the Fire Nation Discord bot (package name kept as "bob").

    app.py         the Discord client: startup, and deciding which messages he answers
    ai/            model providers (Gemini, Groq, OpenAI, Anthropic)
    conversation/  the chat pipeline: prompt building, tool loop, memory
    tools/         the functions the AI can call
    voice/         the voice connection and speech queue
    features/      self-contained features, each loaded as a discord.py extension
"""
