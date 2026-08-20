import aiosqlite
import time
from .config import PRESENCE_HISTORY_RETENTION_DAYS, ALERT_HISTORY_RETENTION_DAYS

DB_PATH = "roblox_monitor.db"

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
                game_id TEXT
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

        # Migration: add presence_type column if it doesn't exist yet
        try:
            await db.execute(
                "ALTER TABLE presence_history ADD COLUMN presence_type INTEGER DEFAULT 2"
            )
        except Exception:
            pass  # Column already exists

        # Migration: add rank, role, username to group_members
        try:
            await db.execute("ALTER TABLE group_members ADD COLUMN rank INTEGER DEFAULT 0")
            await db.execute("ALTER TABLE group_members ADD COLUMN role TEXT DEFAULT ''")
            await db.execute("ALTER TABLE group_members ADD COLUMN username TEXT DEFAULT ''")
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
