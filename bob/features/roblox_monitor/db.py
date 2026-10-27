import logging
import os
import shutil
import time

import aiosqlite

import config
from .config import PRESENCE_HISTORY_RETENTION_DAYS, ALERT_HISTORY_RETENTION_DAYS

logger = logging.getLogger("discord")

# Roblox scans, chat history and memories. Kept in the data folder with the
# rest of the runtime state, so the bot's working directory holds only code.
DB_PATH = os.path.join(config.DATA_DIR, "roblox_monitor.db")
_LEGACY_DB_PATH = "roblox_monitor.db"


def _move_legacy_db() -> str:
    """
    Older versions kept the database in the working directory. It is moved
    once into the data folder; if that fails, the old location keeps being
    used so no history is ever lost.
    """
    if os.path.exists(DB_PATH) or not os.path.exists(_LEGACY_DB_PATH):
        return DB_PATH
    try:
        os.makedirs(config.DATA_DIR, exist_ok=True)
        for suffix in ("", "-wal", "-shm", "-journal"):
            if os.path.exists(_LEGACY_DB_PATH + suffix):
                shutil.move(_LEGACY_DB_PATH + suffix, DB_PATH + suffix)
        logger.info(f"Moved {_LEGACY_DB_PATH} into {config.DATA_DIR}")
        return DB_PATH
    except OSError as e:
        logger.warning(f"Could not move {_LEGACY_DB_PATH} into the data folder, using it where it is: {e}")
        return _LEGACY_DB_PATH


os.makedirs(config.DATA_DIR, exist_ok=True)
DB_PATH = _move_legacy_db()

async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute('''
            CREATE TABLE IF NOT EXISTS group_members (
                user_id INTEGER,
                group_id INTEGER,
                PRIMARY KEY (user_id, group_id)
            )
        ''')
        
        await db.execute('''
            CREATE TABLE IF NOT EXISTS presence_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp INTEGER,
                user_id INTEGER,
                universe_id INTEGER,
                game_id TEXT,
                presence_type INTEGER DEFAULT 2
            )
        ''')
        
        await db.execute('''
            CREATE TABLE IF NOT EXISTS known_games (
                universe_id INTEGER PRIMARY KEY,
                name TEXT,
                last_updated INTEGER
            )
        ''')
        
        await db.execute('''
            CREATE TABLE IF NOT EXISTS alert_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp INTEGER,
                group_id INTEGER,
                universe_id INTEGER,
                alert_level TEXT
            )
        ''')
        
        await db.execute('''
            CREATE TABLE IF NOT EXISTS bot_status (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        ''')
        
        # Conversation AI memory tables
        await db.execute('''
            CREATE TABLE IF NOT EXISTS user_memories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                content TEXT NOT NULL,
                importance INTEGER DEFAULT 5,
                created_at INTEGER,
                last_used_at INTEGER
            )
        ''')
        await db.execute('''
            CREATE TABLE IF NOT EXISTS channel_summaries (
                channel_id INTEGER PRIMARY KEY,
                summary TEXT,
                updated_at INTEGER
            )
        ''')

        # Indexes for fast querying
        await db.execute('CREATE INDEX IF NOT EXISTS idx_group_members_user ON group_members(user_id)')
        await db.execute('CREATE INDEX IF NOT EXISTS idx_group_members_group ON group_members(group_id)')
        await db.execute('CREATE INDEX IF NOT EXISTS idx_presence_history_time ON presence_history(timestamp)')
        await db.execute('CREATE INDEX IF NOT EXISTS idx_presence_history_universe ON presence_history(universe_id)')
        await db.execute('CREATE INDEX IF NOT EXISTS idx_user_memories_uid ON user_memories(user_id)')
        await db.execute('CREATE INDEX IF NOT EXISTS idx_presence_history_composite ON presence_history(timestamp, presence_type, universe_id)')

        # Migration: add presence_type column if it doesn't exist yet
        try:
            await db.execute(
                "ALTER TABLE presence_history ADD COLUMN presence_type INTEGER DEFAULT 2"
            )
        except Exception:
            pass  # Column already exists

        # Migration: add rank, role, username to group_members
        for col, col_def in [("rank", "INTEGER DEFAULT 0"), ("role", "TEXT DEFAULT ''"), ("username", "TEXT DEFAULT ''")]:
            try:
                await db.execute(f"ALTER TABLE group_members ADD COLUMN {col} {col_def}")
            except Exception:
                pass

        await db.commit()

async def cleanup_old_data():
    """Removes old data to respect disk space constraints."""
    async with aiosqlite.connect(DB_PATH) as db:
        now = int(time.time())
        
        presence_cutoff = now - (PRESENCE_HISTORY_RETENTION_DAYS * 24 * 3600)
        await db.execute('DELETE FROM presence_history WHERE timestamp < ?', (presence_cutoff,))
        
        alert_cutoff = now - (ALERT_HISTORY_RETENTION_DAYS * 24 * 3600)
        await db.execute('DELETE FROM alert_history WHERE timestamp < ?', (alert_cutoff,))
        
        await db.commit()
