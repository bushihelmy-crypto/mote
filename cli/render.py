#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Rich-based rendering layer for the interactive REPL — tool-call visualization.

Turns the otherwise-invisible tool-call traffic into colored output: a rounded
:class:`~rich.panel.Panel` per tool call (name highlighted, command / file
content / pattern syntax-highlighted), a compact success/failure line per
result, and live **Markdown** rendering of streamed think tokens.

``rich`` is an **optional** enhancement. When it is not importable the module
degrades gracefully: :func:`build_renderer` returns ``None`` and the REPL keeps
using its existing plain-text path. Streaming uses a single, **transient**
:class:`~rich.live.Live` region that re-renders ``Markdown(buffer)`` as tokens
arrive, cropped to the viewport so it never scrolls (which would duplicate the
reply). :meth:`ConsoleRenderer.end_stream` — called before any other console
output (tool panel, final reply, error) and at the turn boundary — erases the
preview and prints the complete Markdown once into the scrollback. A turn runs
think -> act -> think in order, never concurrently, so at most one ``Live`` is
active at a time; the renderer stays testable by injecting
``Console(file=StringIO())`` and calling ``end_stream``.
"""

from __future__ import annotations

import json
import os
from typing import Any, Optional

try:  # rich is an optional dependency; degrade to plain text when absent.
    from rich import box
    from rich.console import Console
    from rich.live import Live
    from rich.markdown import Markdown
    from rich.panel import Panel
    from rich.syntax import Syntax
    from rich.text import Text

    _HAS_RICH = True
except ImportError:  # pragma: no cover — exercised via monkeypatch in tests
    _HAS_RICH = False


# ---------------------------------------------------------------------------
# Per-tool argument summary + syntax-lexer mapping
# ---------------------------------------------------------------------------
# Which arg holds the "headline" target shown next to the tool name in the panel
# title (e.g. ``Write  scraper.py``). ``None`` => no headline (e.g. Bash).
_HEADLINE_ARG = {
    "Write": "file_path",
    "Edit": "file_path",
    "Read": "file_path",
    "NotebookEdit": "notebook_path",
    "Glob": "pattern",
    "Grep": "pattern",
}

# Which arg holds the body to syntax-highlight, paired with its lexer. ``None``
# lexer means "infer from the file extension of the headline arg". Grep/Glob are
# intentionally absent: their ``pattern`` already shows in the panel title (via
# ``_HEADLINE_ARG``), so re-rendering it as a body would just double-print it.
_BODY = {
    "Bash": ("command", "bash"),
    "terminal": ("input", "bash"),
    "Write": ("content", None),
    "Edit": ("new_string", None),
    "python": ("code", "python"),
}

# Map a file extension to a Pygments lexer name for Write/Edit content.
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

# Failure heuristics for PostToolUse (no structured success flag in payload).
_FAILURE_PREFIXES = ("[PERMISSION DENIED]", "Error", "Traceback", "[PostToolUse]")

_MAX_BODY_LINES = 30
_MAX_RESULT_CHARS = 200


def _lexer_for_path(path: str) -> str:
    _, ext = os.path.splitext(path or "")
    return _EXT_LEXER.get(ext.lower(), "text")


def _truncate_lines(text: str, limit: int) -> str:
    lines = text.splitlines()
    if len(lines) <= limit:
        return text
    kept = lines[:limit]
    kept.append(f"… ({len(lines) - limit} more lines)")
    return "\n".join(kept)


def _first_nonempty_line(text: str) -> str:
    for line in text.splitlines():
        if line.strip():
            return line.strip()
    return ""


class ConsoleRenderer:
    """Rich console renderer: tool-call panels, result lines, token streaming."""

    def __init__(self, console: Optional["Console"] = None):
        # A caller may inject ``Console(file=StringIO(), force_terminal=True)``
        # to capture output for tests; default targets the real terminal.
        self._console = console if console is not None else Console()
        # Live Markdown streaming state. ``_live`` is the active Live region (or
        # None between segments); ``_stream_buffer`` accumulates the current
        # segment's tokens so each refresh re-renders the whole Markdown so far.
        self._live: Optional["Live"] = None
        self._stream_buffer = ""

    # ------------------------------------------------------------------
    # Basic output (REPL reuses these in place of its plain-text helpers)
    # ------------------------------------------------------------------
    def write(self, text: str) -> None:
        self.end_stream()
        self._console.print(text, end="", markup=False, highlight=False)

    def prompt(self, prompt_str: str) -> None:
        self.end_stream()
        self._console.print(prompt_str, end="", markup=False, highlight=False, style="bold cyan")

    def notice(self, text: str) -> None:
        """System notices (^C, interrupt, restore hints) — dim/yellow."""
        self.end_stream()
        self._console.print(text, end="", markup=False, highlight=False, style="yellow")

    def assistant(self, text: str) -> None:
        """The final assistant reply, rendered as Markdown.

        Only reached for a non-streaming provider: when a turn streamed, the
        live Markdown region already rendered the final reply, so the REPL skips
        this call (see Repl._print_new_assistant_messages).
        """
        self.end_stream()
        self._console.print()
        self._console.print(Markdown(text))

    def error(self, text: str) -> None:
        """A failed turn surfaced to the user — red bordered panel."""
        self.end_stream()
        self._console.print()
        self._console.print(
            Panel(
                Text(text, style="red"),
                title=Text("Error", style="bold red"),
                title_align="left",
                box=box.ROUNDED,
                border_style="red",
                expand=False,
            )
        )

    # ------------------------------------------------------------------
    # Token streaming (live, incremental Markdown rendering)
    # ------------------------------------------------------------------
    def stream(self, token: Any) -> None:
        """Append a streamed token and live-render the segment so far as Markdown.

        Lazily opens a :class:`~rich.live.Live` region on the first token of a
        segment, then re-renders ``Markdown(buffer)`` on each token. The region
        is finalized by :meth:`end_stream` (called before any other output and
        at the turn boundary).

        The preview is **cropped** to the viewport (``vertical_overflow="crop"``)
        and **transient**: a re-render must never draw more lines than fit on
        screen, otherwise the terminal scrolls and ``Live`` can no longer move
        the cursor above the top of the screen to erase the prior frame — which
        repaints the whole reply below the old one and duplicates it. The full,
        un-cropped reply is rendered once by :meth:`end_stream` after the
        transient preview is erased.
        """
        text = token if isinstance(token, str) else str(token)
        self._stream_buffer += text
        if self._live is None:
            self._live = Live(
                Markdown(self._stream_buffer),
                console=self._console,
                refresh_per_second=12,
                vertical_overflow="crop",
                transient=True,
            )
            self._live.start()
        else:
            self._live.update(Markdown(self._stream_buffer))

    def end_stream(self) -> None:
        """Finalize the active live stream (if any), printing the full reply.

        Stops the transient preview (erasing its on-screen region), then renders
        the complete accumulated Markdown once into the scrollback so long
        replies survive in full without the per-frame duplication that a tall,
        re-rendered Live region produces. Idempotent and cheap when no stream is
        active. Resets the buffer so the next segment starts fresh.
        """
        if self._live is None:
            return
        live, self._live = self._live, None
        buffer, self._stream_buffer = self._stream_buffer, ""
        try:
            live.stop()  # transient -> erases the in-progress preview region
        finally:
            if buffer.strip():
                self._console.print(Markdown(buffer))

    # ------------------------------------------------------------------
    # Hook entry point
    # ------------------------------------------------------------------
    def on_hook(self, hook_input: Any) -> None:
        """Single hook callback: dispatch by event name. Read-only -> returns None."""
        event = getattr(hook_input, "hook_event_name", None)
        payload = getattr(hook_input, "payload", None) or {}
        try:
            if event == "PreToolUse":
                self._pre_tool(payload)
            elif event == "PostToolUse":
                self._post_tool(payload)
        except Exception:  # noqa: BLE001 — visualization must never break a turn
            pass
        return None

    # ------------------------------------------------------------------
    # Tool-call rendering
    # ------------------------------------------------------------------
    def _pre_tool(self, payload: dict) -> None:
        # Finalize any in-flight streamed think text before the tool panel, so
        # the Live region doesn't fight the panel for the same screen lines.
        self.end_stream()
        name = payload.get("tool_name", "?")
        args = payload.get("tool_input") or {}

        # AskUserQuestion prints its own interactive prompt via the REPL ask
        # channel; rendering it here would double-print.
        if name == "AskUserQuestion":
            return

        headline = ""
        head_arg = _HEADLINE_ARG.get(name)
        if head_arg and isinstance(args.get(head_arg), str):
            headline = args[head_arg]

        title = Text(name, style="bold cyan")
        if headline:
            title.append("  ")
            title.append(headline, style="white")

        body_renderable = self._body_renderable(name, args)
        self._console.print(
            Panel(
                body_renderable if body_renderable is not None else Text(""),
                title=title,
                title_align="left",
                box=box.ROUNDED,
                border_style="cyan",
                expand=False,
            )
        )

    def _body_renderable(self, name: str, args: dict):
        """Build the syntax-highlighted body for a tool panel, or ``None``."""
        spec = _BODY.get(name)
        if spec is not None:
            arg_name, lexer = spec
            value = args.get(arg_name)
            if isinstance(value, str) and value.strip():
                if lexer is None:  # infer from headline file path
                    head_arg = _HEADLINE_ARG.get(name, "")
                    lexer = _lexer_for_path(args.get(head_arg, "") if head_arg else "")
                code = _truncate_lines(value, _MAX_BODY_LINES)
                return Syntax(code, lexer, theme="ansi_dark", word_wrap=True)
            # Known tool but the body arg is empty -> title-only panel.
            return None

        # Unknown tool: pretty-print the args as JSON (truncated).
        if args:
            try:
                dumped = json.dumps(args, indent=2, ensure_ascii=False, default=str)
            except Exception:  # noqa: BLE001
                dumped = str(args)
            dumped = _truncate_lines(dumped, _MAX_BODY_LINES)
            return Syntax(dumped, "json", theme="ansi_dark", word_wrap=True)
        return None

    def _post_tool(self, payload: dict) -> None:
        self.end_stream()
        response = payload.get("tool_response")
        text = response if isinstance(response, str) else ("" if response is None else str(response))
        stripped = text.lstrip()
        failed = stripped.startswith(_FAILURE_PREFIXES)

        if failed:
            detail = _truncate_lines(text.strip(), 5)
            if len(detail) > _MAX_RESULT_CHARS:
                detail = detail[:_MAX_RESULT_CHARS] + "…"
            self._console.print(Text(f"  ✗ {detail}", style="red"))
        else:
            summary = _first_nonempty_line(text)
            if not summary:
                summary = "(no output)"
            elif len(summary) > _MAX_RESULT_CHARS:
                summary = summary[:_MAX_RESULT_CHARS] + "…"
            self._console.print(Text(f"  ✓ {summary}", style="green"))


def build_renderer(out=None) -> Optional[ConsoleRenderer]:
    """Return a :class:`ConsoleRenderer` when rich is available, else ``None``.

    *out* (a file-like, e.g. a ``StringIO`` for tests) is forwarded to the rich
    :class:`~rich.console.Console`; when given, ``force_terminal=True`` keeps
    color/control codes in the captured output.
    """
    if not _HAS_RICH:
        return None
    if out is not None:
        return ConsoleRenderer(Console(file=out, force_terminal=True))
    return ConsoleRenderer()


__all__ = ["ConsoleRenderer", "build_renderer"]
