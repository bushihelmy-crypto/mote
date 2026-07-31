"""Tool-invocation-started projection and argument presentation."""

from __future__ import annotations

import json
import os
from typing import Any, Optional

from mote.contracts.events.tool import ToolInvocationStartedEvent
from mote.product.presentation.events.events import ToolCallStarted

_HEADLINE_ARG = {"Edit": "file_path", "Read": "file_path", "Search": "content"}
_BODY = {
    "Bash": ("command", "bash"),
    "terminal": ("input", "bash"),
    "Edit": ("new_string", None),
    "python": ("code", "python"),
}
_EXT_LEXER = {
    ".py": "python",
    ".js": "javascript",
    ".ts": "typescript",
    ".tsx": "tsx",
    ".jsx": "jsx",
    ".json": "json",
    ".md": "markdown",
    ".sh": "bash",
    ".bash": "bash",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".toml": "toml",
    ".html": "html",
    ".css": "css",
    ".sql": "sql",
    ".go": "go",
    ".rs": "rust",
    ".c": "c",
    ".cpp": "cpp",
    ".java": "java",
}
_MAX_BODY_LINES = 30


def _search_headline(args: dict[str, Any]) -> str:
    for key in ("content", "files"):
        value = args.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return ""


def _lexer_for_path(path: str) -> str:
    _, extension = os.path.splitext(path or "")
    return _EXT_LEXER.get(extension.lower(), "text")


def _truncate_lines(text: str, limit: int) -> str:
    lines = text.splitlines()
    if len(lines) <= limit:
        return text
    return "\n".join([*lines[:limit], f"… ({len(lines) - limit} more lines)"])


def _format_args(args: dict[str, Any]) -> str:
    lines: list[str] = []
    for key, value in args.items():
        if isinstance(value, str):
            text = value
        else:
            try:
                text = json.dumps(value, ensure_ascii=False, default=str)
            except Exception:  # noqa: BLE001 - presentation fallback
                text = str(value)
        if "\n" in text:
            body = "\n".join("    " + line for line in text.splitlines())
            lines.append(f"{key}:\n{body}")
        else:
            lines.append(f"{key}: {text}")
    return "\n".join(lines)


def _body_and_lexer(name: str, args: dict[str, Any]) -> tuple[Optional[str], Optional[str]]:
    spec = _BODY.get(name)
    if spec is not None:
        argument, lexer = spec
        value = args.get(argument)
        if isinstance(value, str) and value.strip():
            if lexer is None:
                headline_argument = _HEADLINE_ARG.get(name, "")
                lexer = _lexer_for_path(args.get(headline_argument, "") if headline_argument else "")
            return _truncate_lines(value, _MAX_BODY_LINES), lexer
        return None, None
    if args:
        return _truncate_lines(_format_args(args), _MAX_BODY_LINES), None
    return None, None


def project_tool_started(
    event: ToolInvocationStartedEvent,
) -> Optional[ToolCallStarted]:
    name = event.tool_name or "?"
    args = event.tool_input
    tool_use_id = event.tool_use_id
    if name == "AskUserQuestion":
        return None
    if name == "RunGraph":
        return ToolCallStarted(
            tool_name=name,
            title=name,
            headline="",
            body=None,
            lexer=None,
            tool_use_id=tool_use_id,
        )
    headline = _search_headline(args) if name == "Search" else ""
    if name != "Search":
        headline_argument = _HEADLINE_ARG.get(name)
        if headline_argument and isinstance(args.get(headline_argument), str):
            headline = args[headline_argument]
    body, lexer = _body_and_lexer(name, args)
    return ToolCallStarted(
        tool_name=name,
        title=name,
        headline=headline,
        body=body,
        lexer=lexer,
        tool_use_id=tool_use_id,
    )


__all__ = ["project_tool_started"]
