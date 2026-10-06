from discord.ext import commands

from .monitor import MonitorTasks


class RobloxMonitorCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.monitor = MonitorTasks(bot)

    async def cog_unload(self):
        self.monitor.cog_unload()


async def setup(bot):
    await bot.add_cog(RobloxMonitorCog(bot))
