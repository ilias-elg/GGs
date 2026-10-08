"""
Memory system for Bob.

Short-term  : in-memory deque per channel (capped, fast, no DB overhead).
Long-term   : SQLite-backed — user memories, server memories, channel summaries.

All async DB operations use aiosqlite for non-blocking I/O.
"""

import time
import re
import asyncio
import aiosqlite
from collections import defaultdict, deque

import config
from bob.features.roblox_monitor.db import DB_PATH

# ---------------------------------------------------------------------------
# In-memory short-term history (channel_id → deque of message dicts)
# ---------------------------------------------------------------------------

MAX_HISTORY = max(40, config.MAX_CONTEXT_MESSAGES + 10)  # messages kept per channel in RAM

_channel_history: dict[int, deque] = defaultdict(lambda: deque(maxlen=MAX_HISTORY))
_channel_web_context: dict[int, str] = {}  # ephemeral per-channel web context
_active_conversations: dict[int, dict] = {}
_ended_at: dict[int, float] = {}
_loaded_channels: set[int] = set()


def activate_conversation(channel_id: int, user_id: int) -> None:
    """Keep a channel open for natural follow-up messages from this user."""
    _active_conversations[channel_id] = {
        "user_id": user_id,
        "expires_at": time.time() + config.CONVERSATION_TTL_SECONDS,
    }


def is_conversation_active(channel_id: int, user_id: int) -> bool:
    if not config.AUTO_FOLLOW_UPS:
        return False
    state = _active_conversations.get(channel_id)
    if not state or state["expires_at"] <= time.time():
        _active_conversations.pop(channel_id, None)
        return False
    return state["user_id"] == user_id


def conversation_open(channel_id: int) -> bool:
    """True while Bob is in a conversation with anyone in this channel."""
    state = _active_conversations.get(channel_id)
    return bool(state) and state["expires_at"] > time.time()


def end_conversation(channel_id: int) -> None:
    _active_conversations.pop(channel_id, None)
    _ended_at[channel_id] = time.time()


def ended_since(channel_id: int, since: float) -> bool:
    """True when the channel's conversation was ended at or after `since` — a reply started before then must not be sent."""
    return _ended_at.get(channel_id, 0.0) >= since


def set_web_context(channel_id: int, text: str) -> None:
    _channel_web_context[channel_id] = text


def get_web_context(channel_id: int) -> str | None:
    return _channel_web_context.get(channel_id)


def add_to_history(
    channel_id: int,
    role: str,
    content: str,
    username: str = "",
    user_id: int = 0,
) -> None:
    """Append a message to the channel's in-memory history."""
    entry = {
        "role": role,
        "content": content,
        "username": username,
        "user_id": user_id,
        "ts": int(time.time()),
    }
    _channel_history[channel_id].append(entry)

    # Keep continuity across bot restarts without blocking Discord's event
    # handler. The database write is best-effort; RAM remains the fast path.
    try:
        loop = asyncio.get_running_loop()
        loop.create_task(_persist_history_entry(channel_id, entry))
    except RuntimeError:
        pass


def get_history(channel_id: int) -> list[dict]:
    """Return recent history as a list ordered oldest → newest."""
    return list(_channel_history[channel_id])


def get_history_for_prompt(channel_id: int) -> list[dict]:
    """
    Return history formatted for the OpenAI messages array.
    User messages are prefixed with the username for multi-user clarity.
    """
    msgs = []
    for entry in _channel_history[channel_id]:
        if entry["role"] == "user" and entry.get("username"):
            msgs.append({"role": "user", "content": f"{entry['username']}: {entry['content']}"})
        else:
            msgs.append({"role": entry["role"], "content": entry["content"]})
    return msgs


async def _persist_history_entry(channel_id: int, entry: dict) -> None:
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "INSERT INTO conversation_messages "
                "(channel_id, role, content, username, user_id, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (channel_id, entry["role"], entry["content"], entry.get("username", ""), entry.get("user_id", 0), entry["ts"]),
            )
            await db.commit()
    except Exception:
        # Persistence should never turn a normal reply into an error.
        pass


async def load_channel_history(channel_id: int) -> None:
    """Load recent persisted messages once, preserving the in-memory fast path."""
    if channel_id in _loaded_channels:
        return
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute(
                "SELECT role, content, username, user_id, created_at "
                "FROM conversation_messages WHERE channel_id = ? "
                "ORDER BY created_at DESC LIMIT ?",
                (channel_id, MAX_HISTORY),
            ) as cur:
                rows = list(reversed(await cur.fetchall()))
        existing = {
            (item["role"], item["content"], item.get("username", ""), item.get("user_id", 0))
            for item in _channel_history[channel_id]
        }
        for role, content, username, user_id, created_at in rows:
            key = (role, content, username or "", user_id or 0)
            if key not in existing:
                _channel_history[channel_id].append({
                    "role": role,
                    "content": content,
                    "username": username or "",
                    "user_id": user_id or 0,
                    "ts": created_at or int(time.time()),
                })
                existing.add(key)
    except Exception:
        pass
    finally:
        _loaded_channels.add(channel_id)


# ---------------------------------------------------------------------------
# Long-term SQLite memory
# ---------------------------------------------------------------------------


async def ensure_memory_tables() -> None:
    """Called once at startup to create/migrate memory tables."""
    async with aiosqlite.connect(DB_PATH) as db:
        # User memories
        await db.execute("""
            CREATE TABLE IF NOT EXISTS user_memories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                content TEXT NOT NULL,
                importance INTEGER DEFAULT 5,
                created_at INTEGER,
                last_used_at INTEGER
            )
        """)
        # Server-scoped memories (guild facts, decisions, configs)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS server_memories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                content TEXT NOT NULL,
                importance INTEGER DEFAULT 5,
                created_at INTEGER,
                last_used_at INTEGER
            )
        """)
        # Channel summaries
        await db.execute("""
            CREATE TABLE IF NOT EXISTS channel_summaries (
                channel_id INTEGER PRIMARY KEY,
                summary TEXT,
                updated_at INTEGER
            )
        """)
        # Durable recent conversation history for continuity after restarts.
        await db.execute("""
            CREATE TABLE IF NOT EXISTS conversation_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                channel_id INTEGER NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                username TEXT,
                user_id INTEGER DEFAULT 0,
                created_at INTEGER
            )
        """)

        # Indexes
        await db.execute("CREATE INDEX IF NOT EXISTS idx_user_memories_uid ON user_memories(user_id)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_server_memories_gid ON server_memories(guild_id)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_conversation_messages_channel ON conversation_messages(channel_id, created_at)")
        await db.commit()


# ---------------------------------------------------------------------------
# Keyword extraction (for relevance scoring)
# ---------------------------------------------------------------------------

_STOPWORDS = frozenset([
    "that", "this", "with", "from", "have", "been", "they", "them",
    "what", "when", "where", "which", "will", "would", "could", "should",
    "about", "your", "their", "just", "like", "also", "there", "some",
    "more", "very", "much", "many", "into", "than", "then", "here",
    "still", "even", "only", "back", "make", "know", "want", "need",
])


def _extract_keywords(text: str) -> list[str]:
    words = re.findall(r'\b[a-zA-Z]{4,}\b', text.lower())
    return [w for w in set(words) if w not in _STOPWORDS]


# ---------------------------------------------------------------------------
# User memories
# ---------------------------------------------------------------------------


async def get_user_memories(
    user_id: int, query_text: str = "", limit: int = 5
) -> list[str]:
    """
    Retrieve relevant user memories.
    If query_text is provided, score memories by keyword overlap.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        keywords = _extract_keywords(query_text) if query_text else []

        if keywords:
            async with db.execute(
                "SELECT id, content, importance, last_used_at FROM user_memories WHERE user_id = ?",
                (user_id,),
            ) as cur:
                rows = await cur.fetchall()

            scored = []
            for mem_id, content, importance, _ in rows:
                content_lower = content.lower()
                score = sum(1 for kw in keywords if kw in content_lower)
                score += (importance or 5) * 0.1
                if score > 0:
                    scored.append((score, mem_id, content))

            scored.sort(reverse=True)
            top = scored[:limit]
        else:
            async with db.execute(
                "SELECT id, content FROM user_memories WHERE user_id = ? "
                "ORDER BY importance DESC, last_used_at DESC LIMIT ?",
                (user_id, limit),
            ) as cur:
                top_rows = await cur.fetchall()
            top = [(0, row[0], row[1]) for row in top_rows]

        if top:
            now = int(time.time())
            ids = [row[1] for row in top]
            await db.executemany(
                "UPDATE user_memories SET last_used_at = ? WHERE id = ?",
                [(now, mid) for mid in ids],
            )
            await db.commit()

        return [row[2] for row in top]


async def save_user_memory(user_id: int, content: str, importance: int = 5) -> None:
    """Persist a new user memory, skipping exact duplicates."""
    now = int(time.time())
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT id FROM user_memories WHERE user_id = ? AND content = ?",
            (user_id, content),
        ) as cur:
            if await cur.fetchone():
                return  # Already saved

        await db.execute(
            "INSERT INTO user_memories (user_id, content, importance, created_at, last_used_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (user_id, content, importance, now, now),
        )
        await db.commit()


async def delete_user_memory(user_id: int, content_fragment: str) -> bool:
    """Delete a user memory matching a content fragment. Returns True if deleted."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT id FROM user_memories WHERE user_id = ? AND content LIKE ?",
            (user_id, f"%{content_fragment}%"),
        ) as cur:
            row = await cur.fetchone()
        if row:
            await db.execute("DELETE FROM user_memories WHERE id = ?", (row[0],))
            await db.commit()
            return True
    return False


async def get_all_user_memories(user_id: int) -> list[str]:
    """Return all memories for a user (for 'what do you remember about me?')."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT content FROM user_memories WHERE user_id = ? ORDER BY importance DESC",
            (user_id,),
        ) as cur:
            rows = await cur.fetchall()
    return [r[0] for r in rows]


# ---------------------------------------------------------------------------
# Server memories (guild-scoped persistent facts)
# ---------------------------------------------------------------------------


async def get_server_memories(
    guild_id: int, query_text: str = "", limit: int = 5
) -> list[str]:
    """Retrieve relevant server-scoped memories."""
    async with aiosqlite.connect(DB_PATH) as db:
        keywords = _extract_keywords(query_text) if query_text else []

        if keywords:
            async with db.execute(
                "SELECT id, content, importance FROM server_memories WHERE guild_id = ?",
                (guild_id,),
            ) as cur:
                rows = await cur.fetchall()

            scored = []
            for mem_id, content, importance in rows:
                content_lower = content.lower()
                score = sum(1 for kw in keywords if kw in content_lower)
                score += (importance or 5) * 0.1
                if score > 0:
                    scored.append((score, mem_id, content))

            scored.sort(reverse=True)
            top = scored[:limit]
        else:
            async with db.execute(
                "SELECT id, content FROM server_memories WHERE guild_id = ? "
                "ORDER BY importance DESC, last_used_at DESC LIMIT ?",
                (guild_id, limit),
            ) as cur:
                top_rows = await cur.fetchall()
            top = [(0, row[0], row[1]) for row in top_rows]

        if top:
            now = int(time.time())
            await db.executemany(
                "UPDATE server_memories SET last_used_at = ? WHERE id = ?",
                [(now, row[1]) for row in top],
            )
            await db.commit()

        return [row[2] for row in top]


async def save_server_memory(guild_id: int, content: str, importance: int = 5) -> None:
    """Persist a server-scoped memory."""
    now = int(time.time())
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT id FROM server_memories WHERE guild_id = ? AND content = ?",
            (guild_id, content),
        ) as cur:
            if await cur.fetchone():
                return
        await db.execute(
            "INSERT INTO server_memories (guild_id, content, importance, created_at, last_used_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (guild_id, content, importance, now, now),
        )
        await db.commit()


# ---------------------------------------------------------------------------
# Channel summaries
# ---------------------------------------------------------------------------


async def get_channel_summary(channel_id: int) -> str | None:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT summary FROM channel_summaries WHERE channel_id = ?", (channel_id,)
        ) as cur:
            row = await cur.fetchone()
    return row[0] if row else None


async def save_channel_summary(channel_id: int, summary: str) -> None:
    now = int(time.time())
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR REPLACE INTO channel_summaries (channel_id, summary, updated_at) "
            "VALUES (?, ?, ?)",
            (channel_id, summary, now),
        )
        await db.commit()
