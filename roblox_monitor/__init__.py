from .db import init_db

async def setup(bot):
    # Initialize database
    await init_db()
    
    # Load the cog
    from .cog import setup as cog_setup
    await cog_setup(bot)
