#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""``TerminalConsumer`` — the rich TUI host for the human ``ViewEvent`` protocol.

This is the §8 migration target for ``cli/render.py``'s ``ConsoleRenderer``: the
rich **styling** stays here (panels, syntax, the incremental-Markdown live region
that commits finalized blocks and keeps only a small erasable tail), while every
**format decision** that used to live here — which arg is a tool's headline, which
is its body+lexer, whether a result failed, the one-line summary — has moved UP
into the :class:`ViewProjector` and now arrives as pre-computed ``ViewEvent``
fields. The consumer just renders them.

DELIBERATE DEVIATION from ARCHITECTURE §2.1's "``_split_committable`` moves to the
projector": the markdown-block-boundary streaming (``_split_committable`` / ``_tail``
/ ``_show_tail`` / ``end_stream``) is a **rich-Live presentation detail**, not a
neutral format decision — a non-rich consumer (JSON-lines, a webhook) has no use
for it. So it stays in the terminal consumer; only the *semantic* derivations went
up. (This is the §9.7 "format once" principle applied honestly: only target-neutral
decisions are hoisted.)

``rich`` is an optional dependency. When absent, :func:`build_terminal_consumer`
returns a plain-text terminal consumer that degrades every event to a simple
``print`` — the app keeps working without color (§9.10).
"""

from __future__ import annotations

import os
import sys
from typing import Any, Optional

from metagpt.cli.common.base import BaseConsumer
from metagpt.cli.common.view import (
    RESULT_KIND_DIFF,
    RESULT_KIND_TABLE,
    TERMINAL_CAPS,
    Capabilities,
)
from metagpt.cli.consumers.render.builders import (
    CONTENT_INDENT as _CONTENT_INDENT,
    RESULT_INDENT as _RESULT_INDENT,
    build_table,
    bullet_row,
    conversation_compacted_text,
    fold_note as _fold_note,
    format_usage_line as _format_usage_line,
    indent as _indent_renderable,
    linkify,
    render_diff,
    render_file_change,
    tool_body_syntax,
    user_message_row,
)
from metagpt.cli.consumers.render.builders import render_image as _render_image
from metagpt.cli.consumers.render.markdown import themed_markdown
from metagpt.cli.consumers.render.terminal_image import detect_image_protocol
from metagpt.cli.consumers.render.palette import (
    BRANCH,
    BULLET,
    CHECK,
    COMPACT,
    CROSS,
    MEDIA,
    NOTE,
    PLAY,
    RETRY,
    SCISSORS,
    SKIP,
    WARN,
    Palette,
)

try:  # rich is optional; degrade to plain text when absent.
    from rich import box
    from rich.console import Console
    from rich.live import Live
    from rich.table import Table
    from rich.text import Text

    _HAS_RICH = True
except ImportError:  # pragma: no cover — exercised via the plain-text fallback
    _HAS_RICH = False


class TerminalConsumer(BaseConsumer):
    """Render the ``ViewEvent`` stream to a rich console (claude-code look).

    The look is a light **bullet + branch** layout, not boxes: ``● Tool(args)``
    for an invocation, ``  ⎿  summary`` for its result, ``●`` marking each
    assistant turn — all keyed off the :mod:`.style` palette. Assistant markdown
    still streams through an incremental live region; the bullet opens the turn
    and continuation lines indent to sit under the content column.
    """

    capabilities: Capabilities = TERMINAL_CAPS

    def __init__(self, console: Optional["Console"] = None):
        self._console = console if console is not None else Console()
        # A native inline-image protocol (Kitty/…) if this terminal speaks one —
        # detected once (the terminal can't change mid-run). ``None`` => fall back
        # to half-block rendering. Detection is a cheap env sniff, no stdin I/O.
        self._image_protocol = detect_image_protocol()
        # Live-markdown streaming state (the trailing, not-yet-finalized block).
        self._live: Optional["Live"] = None
        self._pending = ""
        # The transient LLM-retry countdown region (CC's self-updating "Retrying
        # in Ns" line). Erased the moment any other event arrives — success or
        # final failure — so it never persists in the scrollback.
        self._retry_live: Optional["Live"] = None
        # Whether the current assistant turn has already printed its ``●`` bullet
        # (so continuation blocks indent instead of re-bulleting). Reset on any
        # non-assistant event via ``_end_stream``.
        self._assistant_open = False

    # ------------------------------------------------------------------
    # Small layout helpers (bullet column + indented continuation)
    # ------------------------------------------------------------------
    def _bullet_row(self, glyph: str, renderable: Any, *, style: str):
        """A two-column ``glyph | renderable`` grid (delegates to the shared renderer)."""
        return bullet_row(glyph, renderable, style=style)

    @staticmethod
    def _indent(renderable: Any, spaces: int = _CONTENT_INDENT):
        return _indent_renderable(renderable, spaces)

    # ------------------------------------------------------------------
    # ViewEvent handlers (dispatched by BaseConsumer.handle on ev.kind)
    # ------------------------------------------------------------------
    def on_message_block_started(self, ev: Any) -> None:
        # A streaming region opens; nothing to draw until deltas arrive.
        return None

    def on_message_block_delta(self, ev: Any) -> None:
        self._stream(ev.text)

    def on_reasoning_delta(self, ev: Any) -> None:
        # Think-stream tokens render through the same incremental markdown region.
        self._stream(ev.text)

    def on_message_block_completed(self, ev: Any) -> None:
        if ev.role == "user":
            # The user's own turn — render a ``❯`` prompt block so it reads as a
            # transcript entry (not the assistant's ``●`` bullet).
            self._end_stream()
            if ev.markdown.strip():
                self._console.print()
                self._console.print(user_message_row(ev.markdown))
            return
        if ev.streamed:
            # The live region already rendered it incrementally — just finalize.
            self._end_stream()
            self._show_truncation(ev, spaces=_CONTENT_INDENT)
            return
        # Non-streamed (or downgraded) block: render the markdown fresh, bulleted.
        self._end_stream()
        if ev.markdown.strip():
            self._console.print()
            self._console.print(
                self._bullet_row(BULLET, themed_markdown(ev.markdown), style=Palette.BRAND)
            )
        self._show_truncation(ev, spaces=_CONTENT_INDENT)

    def on_tool_call_started(self, ev: Any) -> None:
        self._end_stream()
        self._console.print()
        line = Text()
        line.append(BULLET + " ", style=Palette.BRAND)
        line.append(ev.title or ev.tool_name, style=f"bold {Palette.BRAND}")
        if ev.headline:
            line.append("(", style=Palette.DIM)
            line.append(ev.headline, style=Palette.DIM)
            line.append(")", style=Palette.DIM)
        self._console.print(line)
        body = tool_body_syntax(ev)
        if body is not None:
            self._console.print(self._indent(body, _RESULT_INDENT))

    def on_tool_call_completed(self, ev: Any) -> None:
        self._end_stream()
        style = Palette.SUCCESS if ev.ok else Palette.ERROR
        summary = ev.summary or ("(no output)" if ev.ok else "failed")
        line = Text()
        line.append("  " + BRANCH + " ", style=Palette.DIM)
        line.append(summary, style=style)
        self._console.print(line)
        # Structured body (already classified by the projector) — render per kind;
        # unknown kinds / no detail fall through to the summary line above.
        detail = getattr(ev, "detail", None)
        if detail:
            kind = getattr(ev, "result_kind", None)
            if kind == RESULT_KIND_DIFF:
                self._console.print(self._indent(render_diff(detail), _RESULT_INDENT))
            elif kind == RESULT_KIND_TABLE:
                table = build_table(detail)
                if table is not None:
                    self._console.print(self._indent(table, _RESULT_INDENT))
            else:
                # Plain result preview (up to ~100 words): show it dimmed under the
                # summary so the user reads real output before the fold note.
                self._console.print(self._indent(linkify(detail, base_style=Palette.DIM), _RESULT_INDENT))
        self._show_truncation(ev, spaces=_RESULT_INDENT)

    def _show_truncation(self, ev: Any, *, spaces: int = _CONTENT_INDENT) -> None:
        """Print a dim footnote when the projector marked the content folded.

        Truncation is a *semantic* property the projector already decided (§7.3);
        the consumer only surfaces the affordance — with ``full_ref`` (the disk
        path / URL the framework persisted) when one is available.
        """
        if not getattr(ev, "content_truncated", False):
            return
        self._console.print(self._indent(_fold_note(ev), spaces))

    def on_media_block(self, ev: Any) -> None:
        # Three-tier image rendering, best-effort first:
        #   1. a native protocol (Kitty/…) → true pixel resolution, and
        #   2. half-block truecolor cells → works in any truecolor terminal, else
        #   3. a labelled reference line (a Web/IM host embeds/uploads instead).
        # A caption line always prints so the transcript records what was shown.
        self._end_stream()
        label = ev.media_kind or "media"
        ref = ev.ref or ev.alt or "(no reference)"
        caption = Text()
        caption.append("  " + BRANCH + " ", style=Palette.DIM)
        caption.append(f"{MEDIA} [{label}] ", style=Palette.BRAND)
        caption.append(ref, style=Palette.DIM)
        self._console.print(caption)

        path = ev.ref or ""
        is_image = label == "image" and bool(path) and os.path.isfile(path)
        if is_image and self._render_native_image(path):
            return
        if is_image:
            image = _render_image(path)
            if image is not None:
                self._console.print(self._indent(image, _RESULT_INDENT))

    def on_file_diff_block(self, ev: Any) -> None:
        # A structured file change (Edit / apply_patch): render a coloured diff
        # synthesized from the ``old``/``new`` full contents. A caption names the
        # file (and whether it was created / deleted) so the transcript records it.
        self._end_stream()
        old = getattr(ev, "old", "") or ""
        new = getattr(ev, "new", "") or ""
        path = getattr(ev, "path", "") or ""
        verb = "created" if not old else ("deleted" if not new else "updated")
        caption = Text()
        caption.append("  " + BRANCH + " ", style=Palette.DIM)
        caption.append(f"{path or 'file'} ", style=Palette.BRAND)
        caption.append(f"({verb})", style=Palette.DIM)
        self._console.print(caption)
        self._console.print(self._indent(render_file_change(old, new, path), _RESULT_INDENT))

    def _render_native_image(self, path: str) -> bool:
        """Emit *path* via the native protocol; True on success, False to fall back.

        Escape sequences bypass rich's renderer (rich would escape them) — we
        write straight to the console's underlying file, then a newline so the
        next transcript line starts cleanly below the image.
        """
        proto = self._image_protocol
        if proto is None:
            return False
        try:
            width = max(1, self._console.size.width - _RESULT_INDENT)
        except Exception:  # noqa: BLE001 — size probing must never break a turn
            width = 80
        seq = proto.encode(path, max_cols=width)
        if not seq:
            return False
        try:
            self._console.file.write(seq + "\n")
            self._console.file.flush()
        except Exception:  # noqa: BLE001 — a write failure degrades to half-block
            return False
        return True

    def on_approval_requested(self, ev: Any) -> None:
        # The interactive decide_approval channel prints the actual y/n prompt;
        # here we only surface a neutral marker so the transcript shows the gate.
        self._end_stream()
        action = ev.action or ev.tool_name or "action"
        line = Text()
        line.append(BULLET + " ", style=Palette.WARNING)
        line.append(f"{WARN} approval required ", style=f"bold {Palette.WARNING}")
        line.append(f"[{ev.risk}] ", style=Palette.DIM)
        line.append(action, style=Palette.WARNING)
        self._console.print(line)

    def on_usage_updated(self, ev: Any) -> None:
        self._end_stream()
        line = _format_usage_line(ev)
        if line:
            self._console.print(self._indent(Text("· " + line, style=Palette.DIM)))

    def on_task_progress(self, ev: Any) -> None:
        self._end_stream()
        status, stage, detail = ev.status, ev.stage or "?", ev.detail
        symbol, style = {
            "running": (PLAY, Palette.BRAND),
            "success": (CHECK, Palette.SUCCESS),
            "failed": (CROSS, Palette.ERROR),
        }.get(status, (SKIP, Palette.WARNING))
        line = Text()
        line.append("  " + symbol + " ", style=style)
        line.append(f"{stage} {status}", style=style)
        if detail and status == "failed":
            line.append(f": {detail}", style=Palette.DIM)
        self._console.print(line)

    def on_notice(self, ev: Any) -> None:
        self._end_stream()
        style = {"warning": Palette.WARNING, "success": Palette.SUCCESS}.get(ev.level, Palette.DIM)
        self._console.print(linkify(ev.text, base_style=style))

    def on_system_reminder(self, ev: Any) -> None:
        # Framework-injected turn context, condensed to a heading summary. Render
        # it dim + ⚑ so it reads as an unobtrusive "what was fed to the model
        # this turn" note, distinct from a command Notice.
        self._end_stream()
        line = Text()
        line.append(NOTE + " ", style=Palette.DIM)
        line.append_text(linkify(getattr(ev, "text", "") or "", base_style=Palette.DIM))
        self._console.print(line)

    def on_conversation_compacted(self, ev: Any) -> None:
        # History was compacted — draw a dim ✻ boundary marker (claude-code look)
        # with a blank line above so it reads as a separator in the transcript.
        self._end_stream()
        self._console.print()
        self._console.print(conversation_compacted_text(ev))

    def on_error_raised(self, ev: Any) -> None:
        self._end_stream()
        self._console.print()
        self._console.print(
            self._bullet_row(BULLET, linkify(ev.text, base_style=Palette.ERROR), style=f"bold {Palette.ERROR}")
        )

    def on_retry_status(self, ev: Any) -> None:
        # A *transient* countdown line (CC's "Retrying in Ns…"): render it in its
        # own erasable Live so the next event (a stream token, tool row, or final
        # error) wipes it — it must never land in the permanent scrollback.
        self._end_stream()  # collapse any open stream block first
        secs = max(0, round((getattr(ev, "delay_ms", 0.0) or 0.0) / 1000.0))
        etype = getattr(ev, "error_type", "") or "error"
        # Span-coloured countdown (claude-code look): the ⟳ glyph + attempt count
        # in warning amber, the error type dimmed, the "Ns 后重试…" tail brand
        # orange so the eye lands on the live countdown.
        text = Text()
        text.append(f"{RETRY} ", style=f"bold {Palette.WARNING}")
        text.append(f"LLM 请求失败（{etype}）", style=Palette.DIM)
        text.append(f" · 第 {ev.attempt}/{ev.max_attempts} 次重试 · ", style=Palette.WARNING)
        text.append(f"{secs}s 后重试…", style=f"bold {Palette.BRAND}")
        # ``_end_stream`` above already wiped any prior retry line, so open a
        # fresh transient Live (erased on the next event via ``_clear_retry``).
        self._retry_live = Live(
            text,
            console=self._console,
            refresh_per_second=12,
            vertical_overflow="crop",
            transient=True,
        )
        self._retry_live.start()

    def _clear_retry(self) -> None:
        """Erase the transient retry line (``stop()`` wipes a ``transient`` Live)."""
        if self._retry_live is not None:
            live, self._retry_live = self._retry_live, None
            live.stop()

    def on_question_asked(self, ev: Any) -> None:
        # The interactive ask channel prints the actual prompt; we only echo a
        # neutral marker so the transcript shows a question was posed.
        self._end_stream()
        line = Text()
        line.append(BULLET + " ", style=Palette.QUESTION)
        line.append("? ", style=f"bold {Palette.QUESTION}")
        line.append(ev.question, style=Palette.QUESTION)
        self._console.print(line)

    def on_session_list_shown(self, ev: Any) -> None:
        # The resumable-session list arrives as structured rows (the projector /
        # driver already picked index/label/preview); render a numbered table so
        # ``/resume <index>`` maps 1:1 to what the human sees.
        self._end_stream()
        if not ev.items:
            self._console.print(Text("  (no sessions)", style=Palette.DIM))
            return
        table = Table(title=ev.title, show_header=True, header_style=f"bold {Palette.BRAND}", box=box.SIMPLE)
        table.add_column("#", style=Palette.BRAND, justify="right")
        table.add_column("Session")
        table.add_column("Updated", style=Palette.DIM)
        table.add_column("Preview", style=Palette.DIM)
        for item in ev.items:
            table.add_row(
                str(item.index),
                item.label or item.session_id,
                item.updated_at or "",
                item.preview or "",
            )
        self._console.print(table)

    def on_transcript_cleared(self, ev: Any) -> None:
        # ``/clear`` — end any open stream, then wipe the terminal scrollback so
        # the human sees a fresh screen (the agent's history was already reset).
        self._end_stream()
        self._console.clear()

    async def aclose(self) -> None:
        self._end_stream()

    # ------------------------------------------------------------------
    # Incremental-Markdown live streaming (ported from render.py)
    # ------------------------------------------------------------------
    @staticmethod
    def _split_committable(text: str) -> tuple[str, str]:
        """Split into ``(finalized, pending)`` at the last safe block boundary.

        A boundary is a blank line outside an open ``` code fence; everything up
        to it cannot change and is safe to commit permanently. The trailing block
        stays pending.
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
        return "\n".join(lines[:last_boundary]), "\n".join(lines[last_boundary + 1 :])

    def _tail(self, text: str) -> str:
        """Crop to the last few lines so the live region never fills the screen."""
        try:
            height = self._console.size.height or 24
        except Exception:  # noqa: BLE001 — size probing must never break a turn
            height = 24
        cap = max(1, height - 2)
        lines = text.split("\n")
        return "\n".join(lines[-cap:])

    def _commit(self, markdown_text: str) -> None:
        """Permanently print a finalized markdown block, opening the ``●`` turn.

        The first committed block of an assistant turn carries the ``●`` bullet
        (via the two-column grid); subsequent blocks indent to sit under it.
        """
        md = themed_markdown(markdown_text)
        if self._assistant_open:
            self._console.print(self._indent(md, _CONTENT_INDENT))
        else:
            self._console.print()
            self._console.print(self._bullet_row(BULLET, md, style=Palette.BRAND))
            self._assistant_open = True

    def _show_tail(self) -> None:
        if not self._pending.strip():
            return
        # The transient tail indents to the content column so it aligns with the
        # committed (bulleted) blocks above; it's erased on the next commit/stop.
        tail = self._indent(themed_markdown(self._tail(self._pending)), _CONTENT_INDENT)
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

    def _stream(self, token: Any) -> None:
        text = token if isinstance(token, str) else str(token)
        if not text:
            return
        # A streamed token means the retry succeeded (deltas bypass _end_stream),
        # so wipe any pending retry countdown before opening the stream region.
        self._clear_retry()
        self._pending += text
        finalized, remainder = self._split_committable(self._pending)
        if finalized.strip():
            self._pending = remainder
            if self._live is not None:
                self._live.update(
                    self._indent(themed_markdown(self._tail(remainder)), _CONTENT_INDENT)
                )
            self._commit(finalized)
        self._show_tail()

    def _end_stream(self) -> None:
        # Any event that finalizes/opens a block also ends a retry countdown:
        # its arrival means the retry resolved (a reply, tool, notice, or the
        # final error), so wipe the transient line first.
        self._clear_retry()
        if self._live is None and not self._pending:
            self._assistant_open = False
            return
        live, self._live = self._live, None
        pending, self._pending = self._pending, ""
        try:
            if live is not None:
                live.stop()
        finally:
            if pending.strip():
                self._commit(pending)
        self._assistant_open = False


class PlainTerminalConsumer(BaseConsumer):
    """Plain-text fallback when ``rich`` is unavailable — no color, no live region.

    Declares ``streaming=False`` so the upstream :class:`CapabilityAdapter`
    buffers deltas into a single ``MessageBlockCompleted`` and this consumer only
    ever prints whole blocks (avoids token-by-token print spam).
    """

    capabilities: Capabilities = Capabilities(streaming=False, markdown=False)

    def __init__(self, out=None):
        self._out = out if out is not None else sys.stdout

    def _print(self, text: str) -> None:
        self._out.write(text + "\n")

    def on_message_block_completed(self, ev: Any) -> None:
        if ev.role == "user":
            if ev.markdown.strip():
                self._print(f"> {ev.markdown}")
            return
        if ev.markdown.strip():
            self._print(ev.markdown)
        self._show_truncation(ev)

    def _show_truncation(self, ev: Any) -> None:
        if not getattr(ev, "content_truncated", False):
            return
        full_ref = getattr(ev, "full_ref", None)
        hidden = getattr(ev, "hidden_lines", 0) or 0
        if full_ref:
            self._print(f"  {SCISSORS} 输出过大已截断，完整见 {full_ref}")
        elif hidden > 0:
            self._print(f"  … +{hidden} 行已折叠")
        else:
            self._print("  … 内容已折叠")

    def on_tool_call_started(self, ev: Any) -> None:
        head = f"  {ev.headline}" if ev.headline else ""
        self._print(f"[{ev.tool_name}]{head}")
        if ev.body:
            self._print(ev.body)

    def on_tool_call_completed(self, ev: Any) -> None:
        mark = "✓" if ev.ok else "✗"
        self._print(f"  {mark} {ev.summary or '(no output)'}")
        self._show_truncation(ev)

    def on_media_block(self, ev: Any) -> None:
        label = ev.media_kind or "media"
        ref = ev.ref or ev.alt or "(no reference)"
        self._print(f"  [{label}] {ref}")

    def on_file_diff_block(self, ev: Any) -> None:
        old = getattr(ev, "old", "") or ""
        new = getattr(ev, "new", "") or ""
        path = getattr(ev, "path", "") or ""
        verb = "created" if not old else ("deleted" if not new else "updated")
        self._print(f"  [{verb}] {path or 'file'}")

    def on_approval_requested(self, ev: Any) -> None:
        action = ev.action or ev.tool_name or "action"
        self._print(f"  approval required [{ev.risk}]: {action}")

    def on_usage_updated(self, ev: Any) -> None:
        line = _format_usage_line(ev)
        if line:
            self._print("  · " + line)

    def on_task_progress(self, ev: Any) -> None:
        self._print(f"  {ev.stage} {ev.status}{(': ' + ev.detail) if ev.detail else ''}")

    def on_notice(self, ev: Any) -> None:
        self._print(ev.text)

    def on_system_reminder(self, ev: Any) -> None:
        self._print(f"{NOTE} {getattr(ev, 'text', '') or ''}")

    def on_conversation_compacted(self, ev: Any) -> None:
        count = getattr(ev, "message_count", 0) or 0
        tail = f" (保留 {count} 条消息)" if count else ""
        self._print(f"{COMPACT} 对话已压缩{tail}")

    def on_retry_status(self, ev: Any) -> None:
        # No erasable region in plain mode — silently swallow the transient
        # retry countdown (printing it would violate "never persist a retry
        # line"). A genuinely exhausted retry still surfaces as a final error.
        return None

    def on_error_raised(self, ev: Any) -> None:
        self._print(f"Error: {ev.text}")

    def on_question_asked(self, ev: Any) -> None:
        self._print(f"? {ev.question}")

    def on_session_list_shown(self, ev: Any) -> None:
        if not ev.items:
            self._print("(no sessions)")
            return
        self._print(ev.title)
        for item in ev.items:
            label = item.label or item.session_id
            updated = f"  {item.updated_at}" if item.updated_at else ""
            preview = f"  {item.preview}" if item.preview else ""
            self._print(f"  {item.index}. {label}{updated}{preview}")

    def on_transcript_cleared(self, ev: Any) -> None:
        # No scrollback to wipe in plain mode; just note the reset.
        self._print("(conversation cleared)")


def build_terminal_consumer(config: Any = None):
    """Return a rich :class:`TerminalConsumer`, or a plain-text one if rich is absent."""
    if _HAS_RICH:
        return TerminalConsumer()
    return PlainTerminalConsumer()


# Self-register on import (the registry imports this module).
try:
    from metagpt.cli.consumers.registry import register_consumer

    register_consumer("terminal", capabilities=TERMINAL_CAPS)(build_terminal_consumer)
except Exception:  # noqa: BLE001 — registry optional during isolated import/tests
    pass


__all__ = ["TerminalConsumer", "PlainTerminalConsumer", "build_terminal_consumer"]
