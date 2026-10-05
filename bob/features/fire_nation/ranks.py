"""
Bot rank model — who may use which merit/knowledge/voice command.

Owner and Fire Lord (second in command) come from configured user IDs; Royalty,
Advisor and HR come from Discord roles of those names. This is separate from
the in-game Fire Nation military ladder described in the knowledge base.
"""

import logging
import os

import discord

import config

logger = logging.getLogger("discord")

RANK_ORDER: dict[str, int] = {
    "owner": 5,
    "second": 4,
    "royalty": 3,
    "advisor": 2,
    "hr": 1,
    "none": 0,
}

RANK_LABELS: dict[str, str] = {
    "owner": "Owner",
    "second": "Fire Lord",
    "royalty": "Royalty",
    "advisor": "Advisor",
    "hr": "HR",
    "none": "none",
}

# ─── Standing-access grants ───────────────────────────────────────────────────
# One Discord user ID per line in data/bot-access.txt, changed with /access or
# by telling Bob in chat. These people can talk to Bob, control his voice and
# put him to sleep, like the Owner and Fire Lord can.

ACCESS_FILE_PATH = os.path.join(config.DATA_DIR, "bot-access.txt")
_LEGACY_ACCESS_FILE_PATH = os.path.join(config.DATA_DIR, "jarvis-access.txt")
access_ids: set[int] = set()


def load_access() -> set[int]:
    global access_ids
    access_ids = set()
    for path in (ACCESS_FILE_PATH, _LEGACY_ACCESS_FILE_PATH):
        try:
            with open(path, encoding="utf-8") as f:
                access_ids |= {int(line) for line in map(str.strip, f) if line.isdigit()}
        except OSError:
            continue
    logger.info(f"Standing-access list loaded ({len(access_ids)} users)")
    return access_ids


def _save_access() -> None:
    os.makedirs(config.DATA_DIR, exist_ok=True)
    with open(ACCESS_FILE_PATH, "w", encoding="utf-8") as f:
        f.writelines(f"{user_id}\n" for user_id in sorted(access_ids))


def grant_access(user_id: int) -> bool:
    """Adds someone to the list. False when they were already on it. Raises OSError if it can't be saved."""
    if user_id in access_ids:
        return False
    access_ids.add(user_id)
    try:
        _save_access()
    except OSError:
        access_ids.discard(user_id)
        raise
    return True


def revoke_access(user_id: int) -> bool:
    """Takes someone off the list. False when they weren't on it. Raises OSError if it can't be saved."""
    if user_id not in access_ids:
        return False
    access_ids.discard(user_id)
    try:
        _save_access()
    except OSError:
        access_ids.add(user_id)
        raise
    return True


# ─── Rank helpers ─────────────────────────────────────────────────────────────


def get_rank(member: discord.abc.User) -> str:
    if member.id in config.OWNER_USER_IDS:
        return "owner"
    if member.id in config.SECOND_IN_COMMAND_USER_IDS:
        return "second"
    roles = getattr(member, "roles", [])
    names = {r.name for r in roles}
    if config.ROYALTY_ROLE_NAME in names:
        return "royalty"
    if config.ADVISOR_ROLE_NAME in names:
        return "advisor"
    if config.HR_ROLE_NAME in names or any(r.id in config.HR_ROLE_IDS for r in roles):
        return "hr"
    return "none"


def fire_nation_title(member: discord.abc.User) -> str | None:
    """
    The rank Bob addresses this person by: their highest role that is Captain /
    Commander or above, or None when they hold none. "Royalty" only names the
    tier, so a specific title held alongside it (Prince, Princess) wins.
    """
    titles = [r for r in getattr(member, "roles", []) if r.name.lower() in config.HR_TITLE_ROLE_NAMES]
    if not titles:
        return None
    specific = [r for r in titles if r.name.lower() != config.ROYALTY_ROLE_NAME.lower()]
    return max(specific or titles, key=lambda r: r.position).name


def can_manage(member: discord.abc.User) -> bool:
    return get_rank(member) in ("owner", "second")


def has_access(member: discord.abc.User) -> bool:
    """Owner, Fire Lord, or anyone on the standing-access list."""
    return can_manage(member) or member.id in access_ids


def rank_at_least(member: discord.abc.User, minimum: str) -> bool:
    return RANK_ORDER[get_rank(member)] >= RANK_ORDER[minimum]


def is_protected_owner(actor_rank: str, target_id: int) -> bool:
    """The Owner is untouchable by the Fire Lord (second in command)."""
    return actor_rank == "second" and target_id in config.OWNER_USER_IDS
