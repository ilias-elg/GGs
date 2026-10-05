"""
Fire Nation knowledge base — editable without touching code.

Sections of fire-nation-knowledge.txt are matched against the user's message
by their ALIASES line, and only the matching sections are added to the prompt.
"""

import logging
import os
import re
import time
from datetime import datetime, timezone

import config

logger = logging.getLogger("discord")

KNOWLEDGE_FILE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fire-nation-knowledge.txt")
# Entries added via /addknowledge live in the persistent data directory, so a
# redeploy that replaces the code doesn't wipe them.
KNOWLEDGE_ADDITIONS_PATH = os.path.join(config.DATA_DIR, "knowledge-additions.txt")

cached_knowledge = ""
# Parsed once per load instead of re-splitting the file on every chat message.
_sections: list[dict] = []

_SECTION_PATTERN = re.compile(r"=== SECTION: (\w+) ===\s*\n([\s\S]*?)=== END SECTION ===")


def _alias_patterns(alias_line: str) -> list[re.Pattern]:
    aliases = (a.strip().lower() for a in alias_line.split(","))
    # skip ultra-common short slang (def, sta, str)
    return [re.compile(rf"\b{re.escape(a)}\b", re.IGNORECASE) for a in aliases if len(a) >= 4]


def _parse_sections(raw: str) -> list[dict]:
    sections = []
    for match in _SECTION_PATTERN.finditer(raw):
        title, body = match.group(1), match.group(2)
        alias_line = re.search(r"ALIASES:\s*(.+)", body, re.IGNORECASE)
        sections.append({
            "title": title,
            "body": f"=== SECTION: {title} ===\n{body}=== END SECTION ===",
            "aliases": _alias_patterns(alias_line.group(1) if alias_line else ""),
        })
    return sections


def _read_optional(path: str) -> str:
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except OSError:
        return ""


def load_knowledge() -> str:
    global cached_knowledge, _sections
    base = _read_optional(KNOWLEDGE_FILE_PATH)
    if not base:
        logger.warning("Could not read fire-nation-knowledge.txt — continuing without it")
    additions = _read_optional(KNOWLEDGE_ADDITIONS_PATH)
    cached_knowledge = f"{base}\n\n{additions}" if additions else base
    _sections = _parse_sections(cached_knowledge)
    logger.info(f"Fire Nation knowledge loaded ({len(cached_knowledge)} chars, {len(_sections)} sections)")
    return cached_knowledge


def get_relevant_knowledge(user_text: str) -> str:
    matches = [s for s in _sections if any(p.search(user_text) for p in s["aliases"])]
    if matches:
        logger.info(f"KB sections injected this turn: {[s['title'] for s in matches]}")
    return "\n\n".join(s["body"] for s in matches)


_STOPWORDS = frozenset([
    "about", "after", "their", "there", "these", "those", "which", "where",
    "while", "would", "should", "could", "other", "every", "being", "because",
    "before", "under", "until", "with", "that", "this", "they", "them", "then",
    "than", "have", "from", "when", "what", "your", "will", "must", "also",
])


def format_knowledge_entry(entry: str, keywords: str | None, added_by: str) -> str:
    """
    Formats a /addknowledge entry as a proper section so the retriever can find
    it. Without an ALIASES line an entry would never be matched on its own.
    """
    if keywords and keywords.strip():
        aliases = keywords
    else:
        words = [w for w in re.findall(r"[a-z][a-z'-]{3,}", entry.lower()) if w not in _STOPWORDS]
        aliases = ", ".join(list(dict.fromkeys(words))[:8])
    title = f"added_{int(time.time() * 1000)}"
    return (
        f"\n=== SECTION: {title} ===\n"
        f"ALIASES: {aliases}\n"
        f"[Added {datetime.now(timezone.utc).isoformat()} by {added_by}] {entry}\n"
        f"=== END SECTION ===\n"
    )


def add_knowledge_entry(entry: str, keywords: str | None, added_by: str) -> int:
    """Appends an entry and reloads. Returns the new total size in characters."""
    with open(KNOWLEDGE_ADDITIONS_PATH, "a", encoding="utf-8") as f:
        f.write(format_knowledge_entry(entry, keywords, added_by))
    return len(load_knowledge())
