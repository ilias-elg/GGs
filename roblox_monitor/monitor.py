import asyncio
import aiosqlite
import time
import logging
import discord
from discord.ext import tasks
from datetime import datetime

from .config import (
    MONITORED_GROUPS, PRESENCE_SCAN_INTERVAL, GROUP_SYNC_INTERVAL,
    PRESENCE_STARTUP_DELAY_SECONDS,
    TARGET_UNIVERSE_ID,
    SPIKE_WINDOW_SECONDS, ALERT_THRESHOLD_INFO, ALERT_THRESHOLD_WARNING,
    ALERT_THRESHOLD_HIGH, ALERT_THRESHOLD_CRITICAL, ALERT_PERCENT_INFO,
    ALERT_COOLDOWN_MINUTES, ALERTS_CHANNEL_ID, ALERT_USER_ID
)
from .db import DB_PATH, cleanup_old_data
from .client import RobloxClient

logger = logging.getLogger('discord')

class MonitorTasks:
    def __init__(self, bot):
        self.bot = bot
        self.client = RobloxClient()
        self.last_scan_time = 0
        self.last_scan_status = "Not started"
        self.tracked_users_count = 0
        self._initial_group_sync_finished = asyncio.Event()
        self._roblox_api_lock = asyncio.Lock()
        
        self.sync_groups.start()
        self.scan_presence.start()
        self.dashboard_updater.start()
        
    def cog_unload(self):
        self.sync_groups.cancel()
        self.scan_presence.cancel()
        self.dashboard_updater.cancel()
        asyncio.create_task(self.client.close())

    @tasks.loop(seconds=GROUP_SYNC_INTERVAL)
    async def sync_groups(self):
        logger.info("Starting group sync...")
        try:
            async with aiosqlite.connect(DB_PATH, timeout=15.0) as db:
                all_users = set()
                
                for group_id in MONITORED_GROUPS:
                    # Do not overlap group sync traffic with the presence API.
                    async with self._roblox_api_lock:
                        members = await self.client.fetch_group_members(group_id)
                    # Use `is None` to check for failure vs empty list
                    if members is None:
                        logger.error(f"Group sync failed for {group_id}, skipping DB wipe")
                        continue
                        
                    # Remove old members
                    await db.execute('DELETE FROM group_members WHERE group_id = ?', (group_id,))
                    
                    if members:
                        # Insert new members with rank, role, username
                        records = [(m['user_id'], group_id, m['rank'], m['role'], m['username']) for m in members]
                        await db.executemany(
                            'INSERT INTO group_members (user_id, group_id, rank, role, username) VALUES (?, ?, ?, ?, ?)',
                            records
                        )
                    
                    # Add to tracked users for presence fetching
                    for m in members:
                        all_users.add(m['user_id'])
                        
                await db.commit()
                self.tracked_users_count = len(all_users)
                logger.info(f"Group sync complete. Tracking {self.tracked_users_count} unique users.")
                
                await cleanup_old_data()
                
        except Exception as e:
            logger.error(f"Error in group sync: {e}")
        finally:
            self._initial_group_sync_finished.set()

    @sync_groups.before_loop
    async def before_sync(self):
        await self.bot.wait_until_ready()

    @tasks.loop(seconds=PRESENCE_SCAN_INTERVAL)
    async def scan_presence(self):
        start_time = time.time()
        try:
            async with aiosqlite.connect(DB_PATH, timeout=15.0) as db:
                # 1. Get all unique users
                async with db.execute('SELECT DISTINCT user_id FROM group_members') as cursor:
                    rows = await cursor.fetchall()
                    user_ids = [r[0] for r in rows]
                
                if not user_ids:
                    self.last_scan_status = "SUCCESS"
                    now = int(time.time())
                    self.last_scan_time = now
                    await db.execute('INSERT OR REPLACE INTO bot_status (key, value) VALUES (?, ?)', ('last_scan_time', str(now)))
                    await db.commit()
                    return
                    
                # 2. Fetch presence
                # A full presence scan owns Roblox API access until all seven
                # batches finish, preventing a group-sync burst from causing
                # a mid-scan throttle.
                async with self._roblox_api_lock:
                    presences = await self.client.fetch_presence(user_ids)
                if not presences:
                    self.last_scan_status = "FAILED"
                    return
                    
                self.last_scan_status = "SUCCESS"
                self.last_scan_time = int(time.time())
                
                # 3. Process and store presence
                now = int(time.time())
                records = []
                new_universes = set()
                
                active_players = 0
                online_players = 0
                null_game_id_count = 0

                for p in presences:
                    ptype = p.get('userPresenceType', 0)
                    user_id = p.get('userId')

                    if ptype == 1:
                        # Online on Roblox but not in a game
                        online_players += 1
                        records.append((now, user_id, None, None, 1))

                    elif ptype == 2:
                        # In-game
                        uid = p.get('universeId')
                        game_id = p.get('gameId')

                        if uid:
                            active_players += 1
                            if game_id is None:
                                null_game_id_count += 1
                            records.append((now, user_id, uid, game_id, 2))
                            new_universes.add(uid)
                
                # Check known games and fetch missing names
                if new_universes:
                    known_ids = []
                    async with db.execute('SELECT universe_id FROM known_games') as cursor:
                        known_ids = [r[0] for r in await cursor.fetchall()]
                    
                    missing_ids = [uid for uid in new_universes if uid not in known_ids]
                    if missing_ids:
                        names = await self.client.fetch_game_names(missing_ids)
                        name_records = [(uid, name, now) for uid, name in names.items()]
                        await db.executemany('INSERT OR REPLACE INTO known_games (universe_id, name, last_updated) VALUES (?, ?, ?)', name_records)
                
                # Insert into history
                if records:
                    await db.executemany(
                        'INSERT INTO presence_history (timestamp, user_id, universe_id, game_id, presence_type) VALUES (?, ?, ?, ?, ?)',
                        records
                    )
                await db.commit()
                
                # 4. Analyze for spikes
                await self.analyze_spikes(db, now)
                
                # 5. Update bot status
                await db.execute('INSERT OR REPLACE INTO bot_status (key, value) VALUES (?, ?)', ('last_scan_time', str(now)))
                await db.commit()
                
                duration = time.time() - start_time
                logger.info(f"Presence scan complete in {duration:.1f}s. Online: {online_players}, In-game: {active_players} (Unassigned server: {null_game_id_count})")
                
        except Exception as e:
            logger.error(f"Error scanning presence: {e}")
            self.last_scan_status = "FAILED"

    async def update_live_dashboard(self, db):
        try:
            async with db.execute("SELECT value FROM bot_status WHERE key = 'live_dash_channel'") as cur:
                ch_row = await cur.fetchone()
            async with db.execute("SELECT value FROM bot_status WHERE key = 'live_dash_msg'") as cur:
                msg_row = await cur.fetchone()
                
            if ch_row and msg_row:
                try:
                    channel = await self.bot.fetch_channel(int(ch_row[0]))
                    msg = await channel.fetch_message(int(msg_row[0]))
                    from .dashboard import build_dashboard_embed
                    embed = await build_dashboard_embed()
                    await msg.edit(embed=embed)
                except discord.NotFound:
                    # Message or channel was deleted, clear it from DB
                    await db.execute("DELETE FROM bot_status WHERE key IN ('live_dash_channel', 'live_dash_msg')")
                    await db.commit()
                except Exception as e:
                    logger.error(f"Error updating live dashboard: {e}")
        except Exception as e:
            logger.error(f"Failed to check live dashboard: {e}")

    @tasks.loop(seconds=20)
    async def dashboard_updater(self):
        try:
            async with aiosqlite.connect(DB_PATH, timeout=15.0) as db:
                await self.update_live_dashboard(db)
        except Exception as e:
            logger.error(f"Error in dashboard_updater: {e}")

    @dashboard_updater.before_loop
    async def before_dashboard_updater(self):
        await self.bot.wait_until_ready()

    @scan_presence.before_loop
    async def before_scan(self):
        await self.bot.wait_until_ready()
        # Let Roblox group-sync traffic settle before the first presence call.
        await self._initial_group_sync_finished.wait()
        await asyncio.sleep(PRESENCE_STARTUP_DELAY_SECONDS)

    async def analyze_spikes(self, db, now):
        window_start = now - SPIKE_WINDOW_SECONDS
        
        # We want to find how many people are playing each universe right now
        # vs how many were playing at the start of the window.
        
        # 1. Current count per universe
        query_current = '''
            SELECT h.universe_id, COUNT(DISTINCT h.user_id) 
            FROM presence_history h
            WHERE h.timestamp = ?
            GROUP BY h.universe_id
        '''
        
        # 2. Previous count per universe (latest timestamp <= window_start)
        query_prev_ts = 'SELECT MAX(timestamp) FROM presence_history WHERE timestamp <= ?'
        
        current_counts = {}
        async with db.execute(query_current, (now,)) as cursor:
            async for row in cursor:
                current_counts[row[0]] = row[1]
                
        prev_ts = None
        async with db.execute(query_prev_ts, (window_start,)) as cursor:
            row = await cursor.fetchone()
            if row:
                prev_ts = row[0]
                
        prev_counts = {}
        if prev_ts:
            query_prev = '''
                SELECT h.universe_id, COUNT(DISTINCT h.user_id) 
                FROM presence_history h
                WHERE h.timestamp = ?
                GROUP BY h.universe_id
            '''
            async with db.execute(query_prev, (prev_ts,)) as cursor:
                async for row in cursor:
                    prev_counts[row[0]] = row[1]
                    
        # Analyze each active universe
        for universe_id, current in current_counts.items():
            if universe_id != TARGET_UNIVERSE_ID:
                continue

            prev = prev_counts.get(universe_id, 0)
            increase = current - prev
            
            if increase >= ALERT_THRESHOLD_INFO or (prev > 0 and current/prev >= ALERT_PERCENT_INFO and increase >= 5):
                await self.process_alert(db, universe_id, current, prev, increase, now)

    async def process_alert(self, db, universe_id, current, prev, increase, now):
        # Determine severity
        level = "INFO"
        if increase >= ALERT_THRESHOLD_CRITICAL:
            level = "CRITICAL"
        elif increase >= ALERT_THRESHOLD_HIGH:
            level = "HIGH"
        elif increase >= ALERT_THRESHOLD_WARNING:
            level = "WARNING"
            
        # Check cooldown
        cooldown_cutoff = now - (ALERT_COOLDOWN_MINUTES * 60)
        
        # Only check cooldown for this universe (ignoring group specific for simplicity, alert is on universe)
        async with db.execute('SELECT MAX(timestamp), alert_level FROM alert_history WHERE universe_id = ? AND timestamp >= ?', (universe_id, cooldown_cutoff)) as cursor:
            row = await cursor.fetchone()
            if row and row[0]:
                last_level = row[1]
                # If we had a recent alert, only escalate if this is a HIGHER level
                levels = {"INFO": 1, "WARNING": 2, "HIGH": 3, "CRITICAL": 4}
                if levels.get(level, 1) <= levels.get(last_level, 1):
                    return # Skip alert due to cooldown
                    
        # Record alert
        await db.execute('INSERT INTO alert_history (timestamp, group_id, universe_id, alert_level) VALUES (?, ?, ?, ?)', (now, 0, universe_id, level))
        await db.commit()
        
        # Gather detailed stats for the embed
        game_name = "Unknown Game"
        async with db.execute('SELECT name FROM known_games WHERE universe_id = ?', (universe_id,)) as cursor:
            row = await cursor.fetchone()
            if row:
                game_name = row[0]
                
        # Group breakdown for this universe right now
        group_breakdown = {}
        server_breakdown = {}
        
        query_details = '''
            SELECT h.user_id, h.game_id, gm.group_id
            FROM presence_history h
            JOIN group_members gm ON h.user_id = gm.user_id
            WHERE h.timestamp = ? AND h.universe_id = ?
        '''
        
        async with db.execute(query_details, (now, universe_id)) as cursor:
            async for row in cursor:
                uid, game_id, group_id = row
                group_breakdown[group_id] = group_breakdown.get(group_id, 0) + 1
                
                if game_id:
                    if game_id not in server_breakdown:
                        server_breakdown[game_id] = set()
                    server_breakdown[game_id].add(uid)
                    
        # Format embed
        embed = discord.Embed(
            title="🚨 PLAYER SPIKE DETECTED",
            color=discord.Color.red() if level in ["HIGH", "CRITICAL"] else discord.Color.orange(),
            timestamp=datetime.now()
        )
        
        embed.add_field(name="EXPERIENCE", value=f"**{game_name}**", inline=False)
        embed.add_field(name="CURRENT PLAYERS", value=str(current), inline=True)
        embed.add_field(name="PREVIOUS COUNT", value=str(prev), inline=True)
        
        inc_str = f"+{increase}"
        if prev > 0:
            pct = int(((current - prev) / prev) * 100)
            inc_str += f" (+{pct}%)"
        embed.add_field(name="INCREASE", value=inc_str, inline=True)
        
        embed.add_field(name="SPIKE WINDOW", value=f"{SPIKE_WINDOW_SECONDS // 60} minutes", inline=True)
        embed.add_field(name="SEVERITY", value=level, inline=True)
        
        # Cross group analysis
        groups_str = ""
        for gid, count in group_breakdown.items():
            name = MONITORED_GROUPS.get(gid, "Unknown")
            groups_str += f"{name}: {count}\n"
        if len(group_breakdown) > 1:
            embed.add_field(name="CROSS-GROUP", value=groups_str or "None", inline=False)
        else:
            embed.add_field(name="GROUP", value=groups_str or "Unknown", inline=False)
            
        # Same server analysis
        max_server_count = 0
        if server_breakdown:
            max_server_count = max(len(users) for users in server_breakdown.values())
            
        if max_server_count > 1:
            embed.add_field(name="SAME SERVER", value=f"{max_server_count} confirmed in same server", inline=False)
        else:
            embed.add_field(name="SAME SERVER", value="UNKNOWN / No concentration", inline=False)
            
        embed.set_footer(text=f"Universe ID: {universe_id}")
        
        # Send to Discord
        if ALERTS_CHANNEL_ID:
            channel = self.bot.get_channel(int(ALERTS_CHANNEL_ID))
            if channel:
                await channel.send(embed=embed)
            else:
                logger.warning("Alerts channel not found!")
                
        if ALERT_USER_ID:
            try:
                # Strip potential quotes/spaces from env var
                clean_id = str(ALERT_USER_ID).strip("'\" ")
                user_id = int(clean_id)
                user = self.bot.get_user(user_id) or await self.bot.fetch_user(user_id)
                if user:
                    await user.send(embed=embed)
            except discord.Forbidden:
                logger.error(f"Cannot DM {ALERT_USER_ID}: User has DMs disabled for server members.")
            except Exception as e:
                logger.error(f"Failed to DM alert to {ALERT_USER_ID}: {e}")
