#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Rich-based rendering layer for the interactive REPL — tool-call visualization.

Turns the otherwise-invisible tool-call traffic into colored output: a rounded
:class:`~rich.panel.Panel` per tool call (name highlighted, command / file
content / pattern syntax-highlighted), a compact success/failure line per
result, and live **Markdown** rendering of streamed think tokens.

``rich`` is an **optional** enhancement. When it is not importable the module
degrades gracefully: :func:`build_renderer` returns ``None`` and the REPL keeps
using its existing plain-text path.

Streaming renders Markdown **incrementally, commit-as-you-go** so it never
duplicates the reply — not even when the reply is taller than the terminal. As
tokens arrive, every *finalized* Markdown block (a paragraph / heading / closed
``` fence — anything before a blank line that is outside an open code fence) is
printed **permanently** into the scrollback the moment it completes; only the
trailing, still-growing block stays in a small **transient** :class:`~rich.live.Live`
region. That live region is cropped to just below the viewport, so it can never
fill the screen and force a scroll — which is what defeats the cursor-up erase
and leaves a stale copy behind. Because committed blocks are plain appends
(never erased) and the live tail is always erasable, the final reply appears
exactly once regardless of length. :meth:`ConsoleRenderer.end_stream` — called
before any other console output (tool panel, final reply, error) and at the turn
boundary — stops the live tail and commits whatever block is still pending. A
turn runs think -> act -> think in order, never concurrently, so at most one
``Live`` is active at a time; the renderer stays testable by injecting
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
        # Live Markdown streaming state. ``_live`` is the active Live region for
        # the trailing, not-yet-finalized block (or None when idle). ``_pending``
        # holds that block's text: finalized blocks are committed to scrollback
        # as they complete, leaving only the growing tail here.
        self._live: Optional["Live"] = None
        self._pending = ""

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
    @staticmethod
    def _split_committable(text: str) -> tuple[str, str]:
        """Split *text* into a ``(finalized, pending)`` pair at the last safe block boundary.

        A boundary is a blank line that lies **outside** an open ``` code fence;
        everything up to (and including) the block before it is finalized — it
        cannot change as more tokens arrive — and is safe to commit permanently.
        The trailing, still-growing block is returned as ``pending``. Fence state
        is tracked so a blank line inside ``` ... ``` is never a boundary, and an
        unclosed fence keeps the whole tail pending. The last element of the
        split is the partial current line (no trailing newline yet), so it is
        never treated as a boundary itself.
        """
        lines = text.split("\n")
        fence = False
        last_boundary: Optional[int] = None
        for k in range(len(lines) - 1):
            line = lines[k]
            if line.strip().startswith("```"):
                fence = not fence
                continue
            if line == "" and not fence:
                last_boundary = k
        if last_boundary is None:
            return "", text
        return "\n".join(lines[:last_boundary]), "\n".join(lines[last_boundary + 1:])

    def _tail(self, text: str) -> str:
        """Crop *text* to the last few lines so the live region never fills the screen.

        Keeping the live tail strictly shorter than the viewport guarantees it
        can never scroll, so its transient erase always succeeds. The earlier
        lines of an in-progress block are not lost: the whole block is committed
        in full once it finalizes (or at :meth:`end_stream`).
        """
        try:
            height = self._console.size.height or 24
        except Exception:  # noqa: BLE001 — size probing must never break a turn
            height = 24
        cap = max(1, height - 2)
        lines = text.split("\n")
        return "\n".join(lines[-cap:])

    def _show_tail(self) -> None:
        """Create or update the live region to show the pending block's tail."""
        if not self._pending.strip():
            return
        tail = Markdown(self._tail(self._pending))
        if self._live is None:
            self._live = Live(
                tail,
                console=self._console,
                refresh_per_second=12,
                vertical_overflow="crop",
                transient=True,
            )
            self._live.start()
        else:
            self._live.update(tail)

    def stream(self, token: Any) -> None:
        """Append a streamed token, committing finalized Markdown blocks as they complete.

        Finalized blocks (everything before the last blank-line boundary outside
        an open code fence) are printed permanently into the scrollback the
        moment they complete; only the trailing, still-growing block stays in a
        small transient :class:`~rich.live.Live` region (see the module docstring
        for why this never duplicates the reply). The pending tail is finalized
        by :meth:`end_stream` (called before any other output and at the turn
        boundary).
        """
        text = token if isinstance(token, str) else str(token)
        self._pending += text
        finalized, remainder = self._split_committable(self._pending)
        if finalized.strip():
            # Shrink the live region to the new (smaller) tail first, then print
            # the finalized block above it. While a Live is running rich routes
            # console prints above the live region, so the committed block lands
            # permanently in the scrollback without disturbing the tail.
            self._pending = remainder
            if self._live is not None:
                self._live.update(Markdown(self._tail(remainder)))
            self._console.print(Markdown(finalized))
        self._show_tail()

    def end_stream(self) -> None:
        """Finalize streaming: stop the live tail and commit the pending block.

        Stops the transient live region (erasing its small on-screen tail — which
        is always erasable because it never filled the screen), then renders the
        remaining pending block once into the scrollback. Idempotent and cheap
        when no stream is active. Resets state so the next segment starts fresh.
        """
        if self._live is None and not self._pending:
            return
        live, self._live = self._live, None
        pending, self._pending = self._pending, ""
        try:
            if live is not None:
                live.stop()  # transient -> erases the (small, erasable) tail
        finally:
            if pending.strip():
                self._console.print(Markdown(pending))

    # ------------------------------------------------------------------
    # Public event-driven entry points (called by the bus subscriber)
    # ------------------------------------------------------------------
    def pre_tool(self, event: Any) -> None:
        """Render a tool-call panel from a PreToolUseEvent."""
        try:
            self._pre_tool({"tool_name": event.tool_name, "tool_input": event.tool_input})
        except Exception:  # noqa: BLE001
            pass

    def post_tool(self, event: Any) -> None:
        """Render a tool-result line from a PostToolUseEvent."""
        try:
            self._post_tool({
                "tool_name": event.tool_name,
                "tool_input": event.tool_input,
                "tool_response": event.tool_response,
            })
        except Exception:  # noqa: BLE001
            pass

    def task_progress(self, event: Any) -> None:
        """Render a bggraph node status change.

        Symbols/colors:
          running   → ▶ (cyan)
          success   → ✓ (green)
          failed    → ✗ (red)
          cancelled/timeout/skipped → ⊘ (yellow)
        """
        self.end_stream()
        status = getattr(event, "status", "")
        stage = getattr(event, "stage", "?")
        detail = getattr(event, "detail", "")

        if status == "running":
            symbol, style = "▶", "cyan"
        elif status == "success":
            symbol, style = "✓", "green"
        elif status == "failed":
            symbol, style = "✗", "red"
        else:
            # cancelled, timeout, skipped, or anything else
            symbol, style = "⊘", "yellow"

        line = f"  {symbol} {stage} {status}"
        if detail and status == "failed":
            line += f": {detail}"
        self._console.print(Text(line, style=style))

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
