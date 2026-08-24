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

from roblox_monitor.db import DB_PATH

# ---------------------------------------------------------------------------
# In-memory short-term history (channel_id → deque of message dicts)
# ---------------------------------------------------------------------------

MAX_HISTORY = 20  # messages kept per channel in RAM

_channel_history: dict[int, deque] = defaultdict(lambda: deque(maxlen=MAX_HISTORY))
_channel_web_context: dict[int, str] = {}  # ephemeral per-channel web context


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
    _channel_history[channel_id].append({
        "role": role,
        "content": content,
        "username": username,
        "user_id": user_id,
        "ts": int(time.time()),
    })


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

        # Indexes
        await db.execute("CREATE INDEX IF NOT EXISTS idx_user_memories_uid ON user_memories(user_id)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_server_memories_gid ON server_memories(guild_id)")
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
