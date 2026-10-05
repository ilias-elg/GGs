"""
Fire Nation features ported from the Jarvis bot: merit ledger, rank roles,
knowledge base, standing orders, spoken voice lines, presence and diagnostics.

Loaded as a discord.py extension from bob/app.py.
"""

import logging
import os

import config

logger = logging.getLogger("discord")


async def setup(bot):
    os.makedirs(config.DATA_DIR, exist_ok=True)

    from . import greetings, knowledge, merit, orders, presence, ranks
    from bob.voice.manager import get_voice_manager

    # A database problem must not take the other features down with it.
    try:
        await merit.ensure_merit_tables()
    except Exception as e:
        logger.error(f"Merit database unavailable — merit commands will fail until it is fixed: {e}")
    knowledge.load_knowledge()
    ranks.load_access()
    greetings.load_greetings()
    orders.load_orders()

    from .cog import FireNationCog
    await bot.add_cog(FireNationCog(bot))

    # Graceful shutdown — leave voice and switch to the offline avatar before
    # the connection closes.
    original_close = bot.close

    async def close():
        try:
            await get_voice_manager(bot).leave_all()
            await presence.on_shutdown(bot)
            await merit.close_pool()
        except Exception as e:
            logger.warning(f"Shutdown cleanup failed: {e}")
        await original_close()

    bot.close = close
