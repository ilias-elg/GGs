"""
Memory system for Bob.

Short-term: in-memory deque per channel (capped, fast, no DB overhead).
Long-term:  SQLite-backed user memories and channel summaries (persistent).
"""

import time
import re
import asyncio
import aiosqlite
from collections import defaultdict, deque

from roblox_monitor.db import DB_PATH

# ---------------------------------------------------------------------------
# In-memory short-term history  (channel_id → deque of message dicts)
# ---------------------------------------------------------------------------

MAX_HISTORY = 20  # messages kept per channel in RAM

_channel_history: dict[int, deque] = defaultdict(lambda: deque(maxlen=MAX_HISTORY))
_channel_web_context: dict[int, str] = {}

def set_web_context(channel_id: int, text: str):
    _channel_web_context[channel_id] = text

def get_web_context(channel_id: int) -> str | None:
    return _channel_web_context.get(channel_id)

def add_to_history(channel_id: int, role: str, content: str,
                   username: str = "", user_id: int = 0):
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
    User messages are prefixed with the username for clarity.
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

async def ensure_memory_tables():
    """Called once at startup to create memory tables if they don't exist."""
    async with aiosqlite.connect(DB_PATH) as db:
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
        await db.execute("""
            CREATE TABLE IF NOT EXISTS channel_summaries (
                channel_id INTEGER PRIMARY KEY,
                summary TEXT,
                updated_at INTEGER
            )
        """)
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_user_memories_uid ON user_memories(user_id)"
        )
        await db.commit()


def _extract_keywords(text: str) -> list[str]:
    """Pull meaningful words (>3 chars) from text for keyword-based retrieval."""
    words = re.findall(r"\b[a-zA-Z]{4,}\b", text.lower())
    stopwords = {
        "that", "this", "with", "from", "have", "been", "they", "them",
        "what", "when", "where", "which", "will", "would", "could", "should",
        "about", "your", "their", "just", "like", "also", "there", "some",
        "more", "very", "much", "many", "into", "than", "then", "here",
    }
    return [w for w in set(words) if w not in stopwords]


async def get_user_memories(user_id: int, query_text: str = "", limit: int = 5) -> list[str]:
    """
    Retrieve relevant user memories.
    If query_text is provided, filter by keyword relevance.
    Returns a list of memory content strings.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        keywords = _extract_keywords(query_text) if query_text else []

        if keywords:
            # Score memories by keyword overlap
            async with db.execute(
                "SELECT id, content, importance, last_used_at FROM user_memories WHERE user_id = ?",
                (user_id,),
            ) as cur:
                rows = await cur.fetchall()

            scored = []
            for row in rows:
                mem_id, content, importance, last_used = row
                content_lower = content.lower()
                score = sum(1 for kw in keywords if kw in content_lower)
                score += (importance or 5) * 0.1  # slight boost for important memories
                if score > 0:
                    scored.append((score, mem_id, content))

            scored.sort(reverse=True)
            top = scored[:limit]
        else:
            # No query — just return most important recent memories
            async with db.execute(
                """SELECT id, content FROM user_memories WHERE user_id = ?
                   ORDER BY importance DESC, last_used_at DESC LIMIT ?""",
                (user_id, limit),
            ) as cur:
                top_rows = await cur.fetchall()
            top = [(0, row[0], row[1]) for row in top_rows]

        # Update last_used_at for retrieved memories
        if top:
            now = int(time.time())
            ids = [row[1] for row in top]
            await db.executemany(
                "UPDATE user_memories SET last_used_at = ? WHERE id = ?",
                [(now, mid) for mid in ids],
            )
            await db.commit()

        return [row[2] for row in top]


async def save_user_memory(user_id: int, content: str, importance: int = 5):
    """Persist a new memory for a user."""
    now = int(time.time())
    async with aiosqlite.connect(DB_PATH) as db:
        # Avoid exact duplicates
        async with db.execute(
            "SELECT id FROM user_memories WHERE user_id = ? AND content = ?",
            (user_id, content),
        ) as cur:
            existing = await cur.fetchone()
        if existing:
            return  # already saved

        await db.execute(
            """INSERT INTO user_memories (user_id, content, importance, created_at, last_used_at)
               VALUES (?, ?, ?, ?, ?)""",
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


async def get_channel_summary(channel_id: int) -> str | None:
    """Get a persisted conversation summary for a channel."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT summary FROM channel_summaries WHERE channel_id = ?", (channel_id,)
        ) as cur:
            row = await cur.fetchone()
    return row[0] if row else None


async def save_channel_summary(channel_id: int, summary: str):
    """Save or replace a channel conversation summary."""
    now = int(time.time())
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """INSERT OR REPLACE INTO channel_summaries (channel_id, summary, updated_at)
               VALUES (?, ?, ?)""",
            (channel_id, summary, now),
        )
        await db.commit()
