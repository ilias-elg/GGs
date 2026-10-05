"""General-purpose tools that are useful outside Discord.

The bot can answer most knowledge questions with the web tools. These tools
cover deterministic tasks and an explicitly opt-in local workspace runner.
The runner is deliberately narrow: it uses one executable invocation (no
shell), is restricted to the configured workspace, and is only visible to
allowlisted users.
"""

from __future__ import annotations

import ast
import asyncio
import math
import os
import re
import shlex
import time
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import config


GENERAL_SCHEMAS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "calculate_expression",
            "description": (
                "Evaluate a mathematical expression accurately. Use this for arithmetic, "
                "percentages, conversions, and comparisons instead of mental math."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "expression": {
                        "type": "string",
                        "description": "A math expression such as '(250 * 1.15) / 4'.",
                    }
                },
                "required": ["expression"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_current_time",
            "description": "Get the current date and time for a city or IANA timezone.",
            "parameters": {
                "type": "object",
                "properties": {
                    "timezone": {
                        "type": "string",
                        "description": "Optional IANA timezone such as Africa/Casablanca or America/New_York. Defaults to the bot's configured local timezone.",
                    }
                },
            },
        },
    },
]

LOCAL_TASK_SCHEMA = {
    "type": "function",
    "function": {
        "name": "run_local_task",
        "description": (
            "Run one explicitly requested local workspace command for the authorized owner. "
            "Use only for tasks that require changing or inspecting files or running a local "
            "development command. Never use shell operators, command chaining, or commands "
            "that expose secrets. This action always requires user confirmation."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "One executable command with arguments; no shell syntax or chaining.",
                },
                "working_directory": {
                    "type": "string",
                    "description": "Optional directory relative to the configured workspace.",
                },
                "timeout_seconds": {
                    "type": "integer",
                    "description": "Timeout from 1 to 120 seconds.",
                },
            },
            "required": ["command"],
        },
    },
}

GENERAL_TOOL_NAMES = frozenset({
    schema["function"]["name"] for schema in GENERAL_SCHEMAS
} | {"run_local_task"})


def _safe_number(value: object) -> int | float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("Only numeric values are allowed")
    if not math.isfinite(value):
        raise ValueError("The result must be finite")
    return value


def _evaluate(node: ast.AST) -> int | float:
    if isinstance(node, ast.Expression):
        return _evaluate(node.body)
    if isinstance(node, ast.Constant):
        return _safe_number(node.value)
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
        value = _evaluate(node.operand)
        return _safe_number(value if isinstance(node.op, ast.UAdd) else -value)
    if isinstance(node, ast.BinOp) and isinstance(
        node.op, (ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv, ast.Mod, ast.Pow)
    ):
        left, right = _evaluate(node.left), _evaluate(node.right)
        result = {
            ast.Add: left + right,
            ast.Sub: left - right,
            ast.Mult: left * right,
            ast.Div: left / right,
            ast.FloorDiv: left // right,
            ast.Mod: left % right,
            ast.Pow: left ** right,
        }[type(node.op)]
        if abs(result) > 1e100:
            raise ValueError("The result is too large")
        return _safe_number(result)
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
        functions = {"abs": abs, "round": round, "min": min, "max": max, "sqrt": math.sqrt}
        function = functions.get(node.func.id)
        if function is None or node.keywords:
            raise ValueError("That function is not allowed")
        return _safe_number(function(*[_evaluate(arg) for arg in node.args]))
    raise ValueError("Only basic arithmetic and safe numeric functions are allowed")


def calculate_expression(expression: str) -> dict:
    expression = expression.strip()
    if not expression or len(expression) > 300:
        return {"error": "Provide a math expression up to 300 characters."}
    try:
        tree = ast.parse(expression, mode="eval")
        result = _evaluate(tree)
        return {"expression": expression, "result": result}
    except (SyntaxError, ValueError, TypeError, ZeroDivisionError, OverflowError) as exc:
        return {"error": f"Could not calculate that: {exc}"}


def get_current_time(timezone: str) -> dict:
    timezone = timezone.strip() or "Africa/Casablanca"
    try:
        now = datetime.now(ZoneInfo(timezone))
    except (ZoneInfoNotFoundError, ValueError):
        return {"error": f"Unknown timezone '{timezone}'. Use an IANA timezone like Africa/Casablanca."}
    return {
        "timezone": timezone,
        "iso": now.isoformat(),
        "readable": now.strftime("%A, %B %d, %Y at %H:%M:%S %Z"),
        "unix": int(time.time()),
    }


def _workspace_path(relative_path: str | None) -> str:
    root = os.path.realpath(config.LOCAL_TASK_WORKSPACE)
    candidate = os.path.realpath(os.path.join(root, relative_path or "."))
    if os.path.commonpath([root, candidate]) != root:
        raise ValueError("Working directory must stay inside the configured workspace")
    return candidate


def _redact_output(value: str) -> str:
    value = re.sub(r"(?i)(api[_-]?key|token|password|secret)\s*[:=]\s*[^\s]+", r"\1=[redacted]", value)
    return value[:6000] + ("\n...[output truncated]" if len(value) > 6000 else "")


def _safe_environment() -> dict[str, str]:
    """Keep normal process settings while omitting obvious secret variables."""
    blocked = re.compile(r"(?i)(api[_-]?key|token|password|secret|private[_-]?key)")
    return {key: value for key, value in os.environ.items() if not blocked.search(key)}


async def run_local_task(args: dict, ctx: dict) -> dict:
    if not config.LOCAL_TASKS_ENABLED:
        return {"error": "Local tasks are disabled. Set LOCAL_TASKS_ENABLED=true to enable them."}
    user_id = int(ctx.get("message").author.id) if ctx.get("message") else 0
    if user_id not in config.LOCAL_TASK_USER_IDS:
        return {"error": "You are not authorized to run local tasks."}

    command = str(args.get("command", "")).strip()
    if not command or len(command) > 500:
        return {"error": "Provide one command up to 500 characters."}
    if re.search(r"(?:&&|\|\||[;|<>`]|\$\(|\n|\r)", command):
        return {"error": "Shell operators and command chaining are not allowed."}

    try:
        argv = shlex.split(command, posix=os.name != "nt")
        cwd = _workspace_path(args.get("working_directory"))
    except ValueError as exc:
        return {"error": str(exc)}
    if not argv:
        return {"error": "Command is empty."}

    timeout = min(max(int(args.get("timeout_seconds", config.LOCAL_TASK_TIMEOUT_SECONDS)), 1), 120)
    try:
        process = await asyncio.create_subprocess_exec(
            *argv,
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=_safe_environment(),
        )
        try:
            output, _ = await asyncio.wait_for(process.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            process.kill()
            await process.communicate()
            return {"error": f"Command timed out after {timeout}s.", "command": command}
        text = _redact_output((output or b"").decode(errors="replace"))
        return {"success": process.returncode == 0, "exit_code": process.returncode, "command": command, "output": text}
    except (FileNotFoundError, PermissionError) as exc:
        return {"error": f"Could not start command: {exc}"}
    except Exception as exc:
        return {"error": f"Local task failed: {exc}"}


async def execute_general_tool(name: str, args: dict, ctx: dict) -> dict:
    if name == "calculate_expression":
        return calculate_expression(str(args.get("expression", "")))
    if name == "get_current_time":
        return get_current_time(str(args.get("timezone", "")))
    if name == "run_local_task":
        return await run_local_task(args, ctx)
    return {"error": f"Unknown general tool: {name}"}
