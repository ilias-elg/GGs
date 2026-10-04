"""
Conversational equivalents of the merit slash commands and the standing-order
controls. Every rank check, DB write, and audit log call goes through
fire_nation/merit.py and fire_nation/orders.py — the exact same functions the
slash-command handlers call — so the rules can't drift between the two entry
points.
"""

import re

import asyncpg
import discord

from fire_nation import merit, orders
from fire_nation.ranks import RANK_ORDER, get_rank

MERIT_SCHEMAS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "award_merit",
            "description": (
                "Awards merits to one or more members. Use type 'bonus' for one or more named members, each "
                "receiving the same 0.1-50 amount (Advisor+ only); use 'exam'/'event' (HR+) or 'raid' "
                "(Advisor+ only) with a required host — the person who receives the merit for running it — "
                "plus any participant usernames. Exams and events (not raids) can also have a cohost, who gets an "
                "extra 0.5 on top of their participant merit — list them in usernames too if they took part."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "merit_type": {"type": "string", "enum": ["exam", "event", "raid", "bonus"]},
                    "usernames": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Usernames/display names/IDs of participants to award. For 'bonus', all listed "
                            "members receive the same amount. For 'exam'/'event'/'raid', these are additional "
                            "participants beyond the host; can be empty if only the host is being credited."
                        ),
                    },
                    "host": {
                        "type": "string",
                        "description": (
                            "Required for 'exam'/'event'/'raid' — the username/display name/ID of whoever "
                            "hosted it. Never assume it is the person you are talking to."
                        ),
                    },
                    "cohost": {
                        "type": "string",
                        "description": "Optional, 'exam'/'event' only. Only set it when the user names a co-host.",
                    },
                    "amount": {
                        "type": "number",
                        "description": "Required only for 'bonus' — amount between 0.1 and 50.",
                    },
                },
                "required": ["merit_type", "usernames"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "remove_merit",
            "description": "Deducts merits from a member. Advisor and above only. Requires a reason.",
            "parameters": {
                "type": "object",
                "properties": {
                    "username": {"type": "string"},
                    "amount": {"type": "number", "description": "0.1-50"},
                    "reason": {"type": "string"},
                },
                "required": ["username", "amount", "reason"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_merits",
            "description": (
                "Reports a specific member's total merit count, or the top-10 leaderboard if no username is given."
            ),
            "parameters": {"type": "object", "properties": {"username": {"type": "string"}}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_merit_history",
            "description": "Returns a member's 10 most recent merit awards with proof links. HR and above only.",
            "parameters": {"type": "object", "properties": {"username": {"type": "string"}}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "verify_recent_merit_actions",
            "description": (
                "Reads the most recent merit ledger entries straight from the database. Use it whenever the "
                "user asks whether something went through, says you made a mistake, or you are unsure what "
                "was actually recorded. HR and above only."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "only_mine": {
                        "type": "boolean",
                        "description": "true = only entries authorized by the person you're talking to (the default); false = anyone's.",
                    },
                    "limit": {"type": "integer", "description": "1-15, default 5."},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "reset_merit_data",
            "description": (
                "Permanently wipes all merit data after exporting a backup to the owner log channel. "
                "DESTRUCTIVE. Owner/Fire Lord only. The user is asked to confirm before it runs."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    },
]

ORDER_SCHEMAS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "add_standing_order",
            "description": (
                "Saves a lasting behaviour rule the user gives you (e.g. 'from now on call Trey Your Majesty', "
                "'never use slang', 'always keep replies short'). Call this whenever the user tells you to "
                "change how you behave going forward — without it the change is NOT remembered. Write the "
                "order as a clear, self-contained instruction to yourself."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "order": {
                        "type": "string",
                        "description": "The rule, as an instruction to yourself, e.g. \"Address Trey as 'Your Majesty'.\"",
                    }
                },
                "required": ["order"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "remove_standing_order",
            "description": (
                "Rescinds one standing order by its number (see Standing Orders in your instructions). Use "
                "when the user says to stop following a rule or drop an order."
            ),
            "parameters": {
                "type": "object",
                "properties": {"number": {"type": "integer", "description": "The order's number, starting at 1."}},
                "required": ["number"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_standing_orders",
            "description": "Lists all standing orders currently in force.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "clear_standing_orders",
            "description": "Rescinds ALL standing orders. The user is asked to confirm before it runs.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
]

FIRE_NATION_TOOL_NAMES = frozenset(
    s["function"]["name"] for s in MERIT_SCHEMAS + ORDER_SCHEMAS
)

# ─── Tool selection ───────────────────────────────────────────────────────────
# get_merits is cheap and read-only, so it is always offered in a server.
# Everything else is only offered when the message is plausibly about it.

_MERIT_INTENT = re.compile(
    r"\b(merits?|bonus|award(ed|s)?|points?|deduct|dock|take away|leaderboard|ranking|top \d+"
    r"|verify|double.?check|went through|go through|did (it|that|you)|mistake|wrong|incorrect|messed up|undo)\b",
    re.IGNORECASE,
)
# Standing orders change Bob's behaviour for everyone, so only the Owner and
# Fire Lord are ever offered the tools that change them.
_ORDER_MIN_RANK = RANK_ORDER["second"]


def get_fire_nation_schemas(text: str, member, offer_all: bool = False) -> list[dict]:
    rank = RANK_ORDER[get_rank(member)] if member else 0
    schemas = [s for s in MERIT_SCHEMAS if s["function"]["name"] == "get_merits"]
    if offer_all or _MERIT_INTENT.search(text):
        schemas = list(MERIT_SCHEMAS)
    if rank >= _ORDER_MIN_RANK and (
        offer_all or orders.ORDER_INTENT.search(text) or orders.ORDER_LIST_INTENT.search(text)
    ):
        schemas += ORDER_SCHEMAS
    return schemas


# ─── Member resolution ────────────────────────────────────────────────────────


def find_member(guild: discord.Guild, query: str) -> discord.Member | str:
    """
    Resolve a member by mention, ID, username or display name. Returns the
    member, or an explanatory string. An abbreviation that matches several
    people is reported as ambiguous instead of guessing, so nobody gets
    merits meant for someone else.
    """
    query = query.strip().lstrip("@")
    mention = re.fullmatch(r"<@!?(\d+)>", query)
    if mention or query.isdigit():
        member = guild.get_member(int(mention.group(1) if mention else query))
        return member or f'I could not locate a member matching "{query}".'

    norm = query.lower()

    def names(m: discord.Member) -> list[str]:
        return [n.lower() for n in (m.name, m.global_name, m.display_name) if n]

    exact = [m for m in guild.members if norm in names(m)]
    if len(exact) == 1:
        return exact[0]
    candidates = exact or [m for m in guild.members if any(n.startswith(norm) for n in names(m))]
    if not candidates:
        return f'I could not locate a member matching "{query}".'
    if len(candidates) == 1:
        return candidates[0]
    shown = ", ".join(f"{m}" + (f" ({m.nick})" if m.nick else "") for m in candidates[:5])
    more = f", and {len(candidates) - 5} more" if len(candidates) > 5 else ""
    return f'I found multiple members matching "{query}" — did you mean: {shown}{more}? Be more specific.'


def _resolve_all(guild: discord.Guild, usernames: list) -> tuple[list[discord.Member], list[str], str | None]:
    """Returns (found, not_found_names, ambiguity_error)."""
    found: list[discord.Member] = []
    not_found: list[str] = []
    for username in usernames:
        result = find_member(guild, str(username))
        if isinstance(result, str):
            if result.startswith("I found multiple"):
                return [], [], result
            not_found.append(str(username))
        elif all(m.id != result.id for m in found):
            found.append(result)
    return found, not_found, None


def _not_found_note(not_found: list[str]) -> str:
    return f" ({len(not_found)} not found: {', '.join(not_found)} — skipped)" if not_found else ""


# ─── Handlers ─────────────────────────────────────────────────────────────────


async def _award_merit(args: dict, message: discord.Message, bot, actor_rank: str) -> str:
    guild, actor = message.guild, message.author
    merit_type = str(args.get("merit_type", "")).lower()
    usernames = args.get("usernames") if isinstance(args.get("usernames"), list) else []
    merit.assert_can_award(actor_rank, merit_type)

    if merit_type == "bonus":
        if not usernames:
            raise merit.MeritError("I need at least one member to award.")
        amount = float(args.get("amount") or 0)
        merit.assert_valid_amount(amount)
        recipients, not_found, ambiguous = _resolve_all(guild, usernames)
        if ambiguous:
            raise merit.MeritError(ambiguous)
        if not recipients:
            raise merit.MeritError("I could not locate any of the members named. Nothing was recorded.")
        for m in recipients:
            merit.assert_not_protected_owner(actor_rank, m.id)

        await merit.record_award(guild.id, recipients, amount, "Bonus (conversational)", actor)
        await merit.audit_award(bot, [(m, amount) for m in recipients], "Bonus", actor)
        return (
            f"Recorded **+{merit.fmt_amount(amount)}** Bonus merit{merit.plural(amount)} for "
            f"**{len(recipients)}** member{'' if len(recipients) == 1 else 's'}"
            f"{_not_found_note(not_found)} — logged for owners."
        )

    # exam / event / raid — host is required and is the one credited
    host_query = str(args.get("host") or "").strip()
    if not host_query:
        raise merit.MeritError("I need a host for that award — that's who receives the merit.")
    host = find_member(guild, host_query)
    if isinstance(host, str):
        raise merit.MeritError(host)
    cohost = None
    cohost_query = str(args.get("cohost") or "").strip()
    if cohost_query:
        cohost = find_member(guild, cohost_query)
        if isinstance(cohost, str):
            raise merit.MeritError(f"Co-host: {cohost}")

    recipients, not_found, ambiguous = _resolve_all(guild, usernames)
    if ambiguous:
        raise merit.MeritError(ambiguous)
    if cohost and (cohost.id == host.id or merit_type not in merit.COHOST_MERIT_TYPES):
        cohost = None
    if all(m.id != host.id for m in recipients):
        recipients.append(host)
    for m in [*recipients, *([cohost] if cohost else [])]:
        merit.assert_not_protected_owner(actor_rank, m.id)

    amount = merit.fixed_merit_amount(merit_type)
    label = merit_type.capitalize()
    awards = [(m, amount) for m in recipients]
    # The co-host bonus is extra: it stacks with the participant merit they
    # get from being listed among the participants.
    if cohost:
        awards.append((cohost, merit.COHOST_BONUS_AMOUNT))
    await merit.record_awards(guild.id, awards, f"{label} (conversational)", actor)
    await merit.audit_award(bot, awards, label, actor, host=host, cohost=cohost)
    cohost_note = ""
    if cohost:
        listed = any(m.id == cohost.id for m in recipients)
        cohost_note = (
            f" Co-host {cohost} received a **+{merit.fmt_amount(merit.COHOST_BONUS_AMOUNT)}** bonus"
            + ("." if listed else " only — they weren't listed as a participant.")
        )
    elif cohost_query and merit_type not in merit.COHOST_MERIT_TYPES:
        cohost_note = " Raids have no co-host bonus, so none was given."
    return (
        f"Recorded **+{merit.fmt_amount(amount)}** {merit_type} merit{merit.plural(amount)} for "
        f"**{len(recipients)}** member{'' if len(recipients) == 1 else 's'} (Host: {host})"
        f"{_not_found_note(not_found)} — logged for owners.{cohost_note}"
    )


async def _remove_merit(args: dict, message: discord.Message, bot, actor_rank: str) -> str:
    merit.assert_can_remove(actor_rank)
    target = find_member(message.guild, str(args.get("username", "")))
    if isinstance(target, str):
        raise merit.MeritError(target)
    amount = float(args.get("amount") or 0)
    merit.assert_valid_amount(amount)
    reason = str(args.get("reason") or "").strip()
    if not reason:
        raise merit.MeritError("I need a reason for the removal.")
    merit.assert_not_protected_owner(actor_rank, target.id)

    await merit.record_removal(message.guild.id, target, amount, reason, message.author)
    await merit.audit_removal(bot, target, amount, reason, message.author)
    return f"Recorded **-{merit.fmt_amount(amount)}** merit{merit.plural(amount)} for {target} — logged for owners."


async def _get_merits(args: dict, message: discord.Message) -> str:
    username = str(args.get("username") or "").strip()
    if username:
        target = find_member(message.guild, username)
        if isinstance(target, str):
            raise merit.MeritError(target)
        total = await merit.get_member_total(target.id)
        return f"{target} currently has **{merit.fmt_amount(total)}** merits."
    leaderboard = await merit.get_leaderboard(10)
    if not leaderboard:
        return "No merits have been recorded yet."
    lines = "\n".join(
        f"{i}. {e['member_tag']} — {merit.fmt_amount(e['total'])}" for i, e in enumerate(leaderboard, start=1)
    )
    return f"Top personnel by merit:\n{lines}"


async def _get_merit_history(args: dict, message: discord.Message, actor_rank: str) -> str:
    merit.assert_can_view_history(actor_rank)
    username = str(args.get("username") or "").strip()
    target = message.author
    if username:
        target = find_member(message.guild, username)
        if isinstance(target, str):
            raise merit.MeritError(target)
    history = await merit.get_member_history(target.id)
    if not history:
        return f"No merit history found for {target}."
    # Matches the tool description ("10 most recent") — /merithistory pages
    # through the full list.
    recent = history[:10]
    more = (
        f"\n…and {len(history) - len(recent)} older entries — use /merithistory for the full list."
        if len(history) > len(recent)
        else ""
    )
    lines = "\n".join(
        f"• {'+' if a['amount'] > 0 else ''}{merit.fmt_amount(a['amount'])} — {a['proof_url']}" for a in recent
    )
    return f"Most recent merit history for {target} ({len(history)} total):\n{lines}{more}"


async def _verify_recent(args: dict, message: discord.Message, actor_rank: str) -> str:
    merit.assert_can_view_history(actor_rank)
    try:
        limit = min(15, max(1, int(args.get("limit") or 5)))
    except (TypeError, ValueError):
        limit = 5
    only_mine = args.get("only_mine") is not False
    rows = await merit.get_recent_actions(limit, message.author.id if only_mine else None)
    mine = " authorized by you" if only_mine else ""
    if not rows:
        return f"The ledger shows no merit entries{mine} — so nothing was recorded."
    lines = "\n".join(
        f"• {'+' if r['amount'] > 0 else ''}{merit.fmt_amount(r['amount'])} → {r['member_tag']} — "
        f"{r['proof_url']} (by {r['awarded_by_tag']}, <t:{r['created_at']}:R>)"
        for r in rows
    )
    noun = "entry" if len(rows) == 1 else "entries"
    return f"Latest {len(rows)} ledger {noun}{mine}, straight from the database:\n{lines}"


async def _reset_merit_data(message: discord.Message, bot, actor_rank: str) -> str:
    merit.assert_can_manage_data(actor_rank)
    entries = await merit.reset_all_data(message.guild.id)
    await merit.audit_reset(bot, entries, message.author)
    return "All merit data has been reset. A full backup was logged to the owner channel first."


def _order_change(actor_rank: str, change) -> dict:
    if actor_rank not in ("owner", "second"):
        return {"error": "Only the Owner or Fire Lord can change standing orders."}
    ok, text = change()
    return {"success": True, "result": text} if ok else {"error": text}


async def execute_fire_nation_tool(name: str, args: dict, ctx: dict) -> dict:
    """Returns {"success": True, "result": text}, or {"error": reason} when nothing was done."""
    message: discord.Message = ctx["message"]
    if not message.guild:
        return {"error": "That only works inside a server."}
    actor_rank = get_rank(message.author)

    if name == "list_standing_orders":
        return {"success": True, "result": orders.format_order_list()}
    if name == "add_standing_order":
        return _order_change(actor_rank, lambda: orders.add_order(str(args.get("order", "")), str(message.author)))
    if name == "remove_standing_order":
        try:
            number = int(args.get("number"))
        except (TypeError, ValueError):
            return {"error": "I need the number of the order to remove."}
        return _order_change(actor_rank, lambda: orders.remove_order(number))
    if name == "clear_standing_orders":
        return _order_change(actor_rank, orders.clear_orders)

    try:
        if name == "award_merit":
            text = await _award_merit(args, message, ctx["bot"], actor_rank)
        elif name == "remove_merit":
            text = await _remove_merit(args, message, ctx["bot"], actor_rank)
        elif name == "get_merits":
            text = await _get_merits(args, message)
        elif name == "get_merit_history":
            text = await _get_merit_history(args, message, actor_rank)
        elif name == "verify_recent_merit_actions":
            text = await _verify_recent(args, message, actor_rank)
        elif name == "reset_merit_data":
            text = await _reset_merit_data(message, ctx["bot"], actor_rank)
        else:
            return {"error": f"Unknown Fire Nation tool: {name}"}
    except merit.MeritError as e:
        return {"error": f"Nothing was done. {e}"}
    except (TypeError, ValueError) as e:
        return {"error": f"Nothing was done — bad arguments: {e}"}
    except (asyncpg.PostgresError, asyncpg.InterfaceError, OSError, TimeoutError) as e:
        return {"error": (
            f"The merit database failed ({str(e)[:150]}). I can't confirm whether it went through — "
            "verify before trying again, so nothing gets doubled."
        )}
    return {"success": True, "result": text}
