"""Workspace file tools for general tasks outside Discord.

All paths are constrained to LOCAL_TASK_WORKSPACE and sensitive files are
never returned to the model. Mutating operations are confirmed by the agent
manager before this module is called.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import config


FILE_SCHEMAS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "list_workspace_files",
            "description": "List files and directories inside the configured workspace.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Relative directory. Defaults to workspace root."},
                    "pattern": {"type": "string", "description": "Filename pattern such as *.py. Defaults to *."},
                    "recursive": {"type": "boolean", "description": "Search nested directories too."},
                    "max_results": {"type": "integer", "description": "Maximum entries, 1-200."},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_workspace_file",
            "description": "Read a text file inside the configured workspace.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Relative file path."},
                    "max_chars": {"type": "integer", "description": "Maximum characters to return, up to 50000."},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_workspace_files",
            "description": "Search text files inside the workspace for a phrase or regular expression.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Text or regular expression to find."},
                    "path": {"type": "string", "description": "Optional relative directory to search."},
                    "max_results": {"type": "integer", "description": "Maximum matching lines, 1-100."},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_workspace_file",
            "description": "Create or overwrite a text file inside the workspace. Always requires confirmation.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Relative file path."},
                    "content": {"type": "string", "description": "Complete text to write."},
                    "mode": {"type": "string", "enum": ["overwrite", "append"], "description": "Write mode. Default overwrite."},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_workspace_file",
            "description": "Delete one file inside the workspace. Always requires confirmation.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Relative file path."},
                },
                "required": ["path"],
            },
        },
    },
]

FILE_TOOL_NAMES = frozenset(schema["function"]["name"] for schema in FILE_SCHEMAS)


def _authorized(ctx: dict) -> bool:
    if not config.LOCAL_TASKS_ENABLED:
        return False
    message = ctx.get("message")
    if not message:
        return False
    user_id = int(message.author.id)
    if user_id in config.LOCAL_TASK_USER_IDS:
        return True
    guild_permissions = getattr(message.author, "guild_permissions", None)
    return bool(guild_permissions and guild_permissions.administrator)


def _root() -> Path:
    return Path(os.path.realpath(config.LOCAL_TASK_WORKSPACE))


def _resolve(relative_path: str, *, allow_root: bool = True) -> Path:
    root = _root()
    candidate = Path(os.path.realpath(os.path.join(root, relative_path or ".")))
    if os.path.commonpath([str(root), str(candidate)]) != str(root):
        raise ValueError("Path must stay inside the configured workspace")
    if not allow_root and candidate == root:
        raise ValueError("The workspace root is not a file")
    return candidate


def _sensitive(path: Path) -> bool:
    name = path.name.lower()
    return (
        name in {".env", ".env.local", ".env.production"}
        or any(part in name for part in ("secret", "password", "token"))
        or path.suffix.lower() in {".pem", ".key", ".p12", ".pfx"}
    )


def _require_access(ctx: dict) -> dict | None:
    if not config.LOCAL_TASKS_ENABLED:
        return {"error": "Workspace tools are disabled. Set LOCAL_TASKS_ENABLED=true."}
    if not _authorized(ctx):
        return {"error": "You are not authorized to use workspace tools."}
    return None


def _append_text(path: Path, content: str) -> None:
    with path.open("a", encoding="utf-8") as handle:
        handle.write(content)


async def list_workspace_files(args: dict, ctx: dict) -> dict:
    if error := _require_access(ctx):
        return error
    try:
        base = _resolve(str(args.get("path", "")))
        pattern = str(args.get("pattern", "*")) or "*"
        recursive = bool(args.get("recursive", False))
        limit = min(max(int(args.get("max_results", 100)), 1), 200)
        iterator = base.rglob(pattern) if recursive else base.glob(pattern)
        entries = []
        for path in sorted(iterator, key=lambda item: str(item).lower()):
            if len(entries) >= limit or _sensitive(path):
                continue
            entries.append({
                "path": str(path.relative_to(_root())),
                "type": "directory" if path.is_dir() else "file",
                "size": path.stat().st_size if path.is_file() else None,
            })
        return {"root": str(base.relative_to(_root()) or "."), "entries": entries}
    except (OSError, ValueError) as exc:
        return {"error": str(exc)}


async def read_workspace_file(args: dict, ctx: dict) -> dict:
    if error := _require_access(ctx):
        return error
    try:
        path = _resolve(str(args.get("path", "")), allow_root=False)
        if _sensitive(path):
            return {"error": "Sensitive files cannot be read through Bob."}
        if not path.is_file():
            return {"error": f"File not found: {args.get('path')}"}
        limit = min(max(int(args.get("max_chars", 20000)), 1), 50000)
        content = await asyncio.to_thread(path.read_text, encoding="utf-8", errors="replace")
        return {"path": str(path.relative_to(_root())), "content": content[:limit], "truncated": len(content) > limit}
    except (OSError, UnicodeError, ValueError) as exc:
        return {"error": str(exc)}


async def search_workspace_files(args: dict, ctx: dict) -> dict:
    if error := _require_access(ctx):
        return error
    query = str(args.get("query", ""))
    if not query or len(query) > 300:
        return {"error": "Search query must be between 1 and 300 characters."}
    try:
        import re
        expression = re.compile(query, re.IGNORECASE)
    except re.error:
        expression = None
    try:
        base = _resolve(str(args.get("path", "")))
        limit = min(max(int(args.get("max_results", 50)), 1), 100)
        matches = []
        for path in base.rglob("*"):
            if len(matches) >= limit or not path.is_file() or _sensitive(path):
                continue
            try:
                text = await asyncio.to_thread(path.read_text, encoding="utf-8", errors="replace")
            except (OSError, UnicodeError):
                continue
            for line_number, line in enumerate(text.splitlines(), 1):
                found = bool(expression.search(line)) if expression else query.lower() in line.lower()
                if found:
                    matches.append({"path": str(path.relative_to(_root())), "line": line_number, "text": line[:500]})
                    if len(matches) >= limit:
                        break
        return {"query": query, "matches": matches}
    except (OSError, ValueError) as exc:
        return {"error": str(exc)}


async def write_workspace_file(args: dict, ctx: dict) -> dict:
    if error := _require_access(ctx):
        return error
    try:
        path = _resolve(str(args.get("path", "")), allow_root=False)
        if _sensitive(path):
            return {"error": "Sensitive files cannot be written through Bob."}
        content = str(args.get("content", ""))
        if len(content) > 200000:
            return {"error": "File content is limited to 200,000 characters."}
        mode = str(args.get("mode", "overwrite")).lower()
        path.parent.mkdir(parents=True, exist_ok=True)
        if mode == "append":
            await asyncio.to_thread(_append_text, path, content)
        else:
            await asyncio.to_thread(path.write_text, content, encoding="utf-8")
        return {"success": True, "path": str(path.relative_to(_root())), "bytes": len(content.encode("utf-8"))}
    except (OSError, ValueError) as exc:
        return {"error": str(exc)}


async def delete_workspace_file(args: dict, ctx: dict) -> dict:
    if error := _require_access(ctx):
        return error
    try:
        path = _resolve(str(args.get("path", "")), allow_root=False)
        if _sensitive(path):
            return {"error": "Sensitive files cannot be deleted through Bob."}
        if not path.is_file():
            return {"error": f"File not found: {args.get('path')}"}
        await asyncio.to_thread(path.unlink)
        return {"success": True, "path": str(path.relative_to(_root()))}
    except (OSError, ValueError) as exc:
        return {"error": str(exc)}


async def execute_filesystem_tool(name: str, args: dict, ctx: dict) -> dict:
    handlers = {
        "list_workspace_files": list_workspace_files,
        "read_workspace_file": read_workspace_file,
        "search_workspace_files": search_workspace_files,
        "write_workspace_file": write_workspace_file,
        "delete_workspace_file": delete_workspace_file,
    }
    handler = handlers.get(name)
    return await handler(args, ctx) if handler else {"error": f"Unknown workspace tool: {name}"}
