"""
Custom voice greetings, one per person. Set with /voice greeting. Stored in
the data directory so they survive restarts and redeploys. People without a
custom line get the default.
"""

import json
import logging
import os
import re

import config

logger = logging.getLogger("discord")

DEFAULT_GREETING = "Welcome back."
MAX_GREETING_LENGTH = 200

GREETINGS_FILE = os.path.join(config.DATA_DIR, "voice-greetings.json")
_greetings: dict[str, str] = {}


def load_greetings() -> None:
    global _greetings
    try:
        with open(GREETINGS_FILE, encoding="utf-8") as f:
            parsed = json.load(f)
        _greetings = parsed if isinstance(parsed, dict) else {}
        logger.info(f"Custom voice greetings loaded ({len(_greetings)})")
    except (OSError, ValueError):
        _greetings = {}


def _persist() -> bool:
    try:
        with open(GREETINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(_greetings, f, indent=2)
        return True
    except OSError as e:
        logger.error(f"Failed to save voice-greetings.json: {e}")
        return False


def set_greeting(user_id: int, line: str) -> bool:
    """Saves a greeting. Returns False if it couldn't be written to disk."""
    _greetings[str(user_id)] = line
    return _persist()


def clear_greeting(user_id: int) -> bool:
    """Removes a custom greeting. Returns whether one existed."""
    if _greetings.pop(str(user_id), None) is None:
        return False
    _persist()
    return True


def list_greetings() -> list[tuple[str, str]]:
    return list(_greetings.items())


def greeting_for(user_id: int, display_name: str) -> str:
    """The line to say when this person joins — their custom one, or the default."""
    line = _greetings.get(str(user_id), DEFAULT_GREETING)
    return re.sub(r"\{name\}", lambda _: display_name, line, flags=re.IGNORECASE)
