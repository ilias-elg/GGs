import discord
from discord.ext import commands
from discord import app_commands
import aiosqlite
import time

from .db import DB_PATH
from .monitor import MonitorTasks
from .config import MONITORED_GROUPS

class RobloxMonitorCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.monitor = MonitorTasks(bot)

    async def cog_unload(self):
        self.monitor.cog_unload()

    @app_commands.command(name="status", description="Shows overall monitoring status.")
    async def status(self, interaction: discord.Interaction):
        embed = discord.Embed(title="Roblox Monitor Status", color=discord.Color.blue())
        
        embed.add_field(name="Tracked Users", value=str(self.monitor.tracked_users_count), inline=True)
        
        status_text = self.monitor.last_scan_status
        if self.monitor.last_scan_time > 0:
            ago = int(time.time()) - self.monitor.last_scan_time
            status_text += f" ({ago}s ago)"
            
        embed.add_field(name="Last Scan", value=status_text, inline=True)
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="groups", description="Shows all monitored groups.")
    async def groups(self, interaction: discord.Interaction):
        embed = discord.Embed(title="Monitored Groups", color=discord.Color.green())
        
        async with aiosqlite.connect(DB_PATH) as db:
            for group_id, name in MONITORED_GROUPS.items():
                async with db.execute('SELECT COUNT(*) FROM group_members WHERE group_id = ?', (group_id,)) as cursor:
                    count = (await cursor.fetchone())[0]
                    embed.add_field(name=name, value=f"{count} members tracked\nID: {group_id}", inline=False)
                    
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="games", description="Shows current games being played by monitored members.")
    async def games(self, interaction: discord.Interaction):
        await interaction.response.defer()
        
        async with aiosqlite.connect(DB_PATH) as db:
            query = '''
                SELECT h.universe_id, COUNT(DISTINCT h.user_id), kg.name
                FROM presence_history h
                LEFT JOIN known_games kg ON h.universe_id = kg.universe_id
                WHERE h.timestamp = (SELECT MAX(timestamp) FROM presence_history)
                GROUP BY h.universe_id
                ORDER BY COUNT(DISTINCT h.user_id) DESC
                LIMIT 10
            '''
            
            embed = discord.Embed(title="Top Games Currently Played", color=discord.Color.purple())
            
            async with db.execute(query) as cursor:
                rows = await cursor.fetchall()
                if not rows:
                    embed.description = "No members are currently playing any games, or data is missing."
                else:
                    for uid, count, name in rows:
                        game_name = name or f"Universe {uid}"
                        embed.add_field(name=game_name, value=f"{count} players", inline=False)
                        
            await interaction.followup.send(embed=embed)

    @app_commands.command(name="health", description="Shows technical health of the monitor.")
    async def health(self, interaction: discord.Interaction):
        embed = discord.Embed(title="System Health", color=discord.Color.gold())
        
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute('SELECT COUNT(*) FROM presence_history') as cursor:
                hist_count = (await cursor.fetchone())[0]
            
            embed.add_field(name="Database", value=f"OK\n{hist_count} presence records", inline=False)
            
        embed.add_field(name="Last API Scan", value=self.monitor.last_scan_status, inline=False)
        embed.add_field(name="Users in memory", value=str(self.monitor.tracked_users_count), inline=False)
        
        await interaction.response.send_message(embed=embed)

async def setup(bot):
    await bot.add_cog(RobloxMonitorCog(bot))
