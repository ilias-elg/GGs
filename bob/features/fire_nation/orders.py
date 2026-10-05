"""
Standing orders — lasting behaviour rules the Owner / Fire Lord give Bob in
plain words ("from now on, call Trey 'Your Majesty'"). They're saved to disk
and added to his instructions on every message, so they survive restarts
without touching code. Every order is sent with every AI request, so the
total size is capped to keep the per-message token cost bounded.
"""

import json
import logging
import os
import re
import time

import config

logger = logging.getLogger("discord")

MAX_ORDERS = 25
MAX_ORDER_LENGTH = 300
MAX_TOTAL_CHARS = 3000  # ≈ 750 tokens on every message, worst case

ORDERS_FILE = os.path.join(config.DATA_DIR, "standing-orders.json")
_orders: list[dict] = []

# Phrasing that sets or changes a lasting behaviour ("from now on…",
# "never…", "stop calling…"). Used to decide when to offer the order tools.
ORDER_INTENT = re.compile(
    r"\b(from now on|going forward|henceforth|standing order|new (rule|order)|always|never|every ?time|each time"
    r"|remember to|stop (calling|saying|doing|using|being|adding|mentioning|telling|explaining)|quit"
    r"|(you )?(don'?t|do not) (have to|need to|ever|call|say|use|mention|add|tell|explain|do that)|no need to"
    r"|too (long|much|wordy|formal|verbose)|(way )?shorter|cut (it|that|the) |that'?s annoying|annoying)\b",
    re.IGNORECASE,
)
ORDER_LIST_INTENT = re.compile(r"\b(orders?|rules?|rescind|revoke|protocols?)\b", re.IGNORECASE)


def load_orders() -> None:
    global _orders
    try:
        with open(ORDERS_FILE, encoding="utf-8") as f:
            parsed = json.load(f)
        _orders = [o for o in parsed if isinstance(o, dict) and isinstance(o.get("text"), str)]
        logger.info(f"Standing orders loaded ({len(_orders)})")
    except (OSError, ValueError, TypeError):
        _orders = []


def _persist() -> bool:
    try:
        with open(ORDERS_FILE, "w", encoding="utf-8") as f:
            json.dump(_orders, f, indent=2)
        return True
    except OSError as e:
        logger.error(f"Failed to save standing-orders.json: {e}")
        return False


def _total_chars() -> int:
    return sum(len(o["text"]) for o in _orders)


def add_order(text: str, added_by: str) -> tuple[bool, str]:
    clean = " ".join(text.split())
    if not clean:
        return False, "That order is empty."
    if len(clean) > MAX_ORDER_LENGTH:
        return False, f"Orders are limited to {MAX_ORDER_LENGTH} characters — that one is {len(clean)}. Shorten it."
    if any(o["text"].lower() == clean.lower() for o in _orders):
        return False, "That exact order is already on file."
    if len(_orders) >= MAX_ORDERS:
        return False, f"Already holding the maximum of {MAX_ORDERS} standing orders. Remove one first."
    if _total_chars() + len(clean) > MAX_TOTAL_CHARS:
        return False, (
            f"That would put the standing orders over {MAX_TOTAL_CHARS} characters — they're sent with "
            "every message, so they're capped. Remove or shorten one first."
        )

    _orders.append({"text": clean, "added_by": added_by, "added_at": int(time.time())})
    if not _persist():
        _orders.pop()
        return False, "Couldn't save that order to disk — nothing changed. Check the logs."
    return True, (
        f'Standing order #{len(_orders)} recorded: "{clean}". '
        "It applies from the next message onward, in every conversation."
    )


def remove_order(number: int) -> tuple[bool, str]:
    if not isinstance(number, int) or number < 1 or number > len(_orders):
        if not _orders:
            return False, "There are no standing orders to remove."
        return False, f"There's no order #{number} — there are {len(_orders)} (1–{len(_orders)})."
    removed = _orders.pop(number - 1)
    if not _persist():
        _orders.insert(number - 1, removed)
        return False, "Couldn't save the change to disk — the order is still in force."
    renumbered = " The remaining orders have been renumbered." if _orders else ""
    return True, f'Standing order #{number} rescinded: "{removed["text"]}".{renumbered}'


def clear_orders() -> tuple[bool, str]:
    global _orders
    if not _orders:
        return False, "There are no standing orders to clear."
    previous, count = _orders, len(_orders)
    _orders = []
    if not _persist():
        _orders = previous
        return False, "Couldn't save the change to disk — all orders are still in force."
    return True, f"All {count} standing order{'' if count == 1 else 's'} rescinded."


def format_order_list() -> str:
    if not _orders:
        return "There are no standing orders at present."
    lines = [
        f"**{i}.** {o['text']} — *by {o['added_by']}, <t:{o['added_at']}:R>*"
        for i, o in enumerate(_orders, start=1)
    ]
    header = f"**Standing orders ({len(_orders)}/{MAX_ORDERS}, {_total_chars()}/{MAX_TOTAL_CHARS} chars):**"
    return header + "\n" + "\n".join(lines)


def orders_prompt_block() -> str:
    """The block added to the system prompt. Empty when there are no orders."""
    if not _orders:
        return ""
    return (
        "\n\n## Standing Orders (set by the Owner / Fire Lord — follow them in every reply)\n"
        "These override your default style and habits. They never override honesty, rank/permission "
        "checks, or the verified speaker rank — if an order would require lying, faking an action, or "
        "granting someone authority they don't have, ignore that part and say so.\n"
        + "\n".join(f"{i}. {o['text']}" for i, o in enumerate(_orders, start=1))
    )
