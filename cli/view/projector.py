#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""``ViewProjector`` — the single fold point ``AgentEvent → ViewEvent`` (窄腰).

This is where the old ``render.py``'s per-frontend recompute moves UP and happens
**once**. The projector subscribes to the unified ``AgentEvent`` spine (as an
:class:`ObservationSubscriber`) and folds each event into zero-or-more
``ViewEvent``\\s — *deciding once* which tool arg is the headline, which is the
body and its lexer, whether a result failed, and the one-line summary. Those land
on the human protocol as neutral, pre-computed fields; no consumer re-derives them
(ARCHITECTURE §2.2.1 / §4).

``ViewProjector.project`` is the **pure** fold (``AgentEvent → list[ViewEvent]``):
no I/O, no consumer knowledge; trivially unit-testable in isolation. The
**plumbing** that fans this fold out to many consumers (capability downgrade per
consumer, sync/async dispatch) lives in the reusable
:class:`mote.cli.contracts.base.BaseProjector`, into which the host injects this
``ViewProjector`` as its concrete fold.
"""

from __future__ import annotations

import json
import os
from typing import Any, List, Optional, cast

from mote.cli.contracts.view.events import (
    RESULT_KIND_DIFF,
    RESULT_KIND_PLAIN,
    ConversationCompacted,
    FileDiffBlock,
    MediaBlock,
    MessageBlockCompleted,
    MessageBlockDelta,
    MessageBlockStarted,
    RetryStatus,
    SystemReminder,
    TaskProgress,
    ToolCallCompleted,
    ToolCallStarted,
    ViewEvent,
)
from mote.cli.view.reminders import _is_system_reminder, _summarize_reminder
from mote.cli.view.summaries import _result_summary
from mote.common.events.types import (
    COMPACTION_CHECKPOINT,
    LLM_ERROR,
    LLM_RETRY,
    LLM_STREAM_DELTA,
    LLM_STREAM_END,
    MESSAGE_APPENDED,
    POST_TOOL_USE,
    PRE_TOOL_USE,
    TASK_PROGRESS,
)
from mote.common.text import PERSISTED_OUTPUT_OPEN

# ---------------------------------------------------------------------------
# Tool-formatting tables — ported VERBATIM from ``cli/render.py``.
# These used to be re-applied per frontend; the projector now applies them once
# and ships the result as neutral ``ToolCallStarted`` fields.
# ---------------------------------------------------------------------------

# Which arg holds the "headline" target shown next to the tool name
# (e.g. ``Write  scraper.py``). Absent => no headline (e.g. Bash).
_HEADLINE_ARG = {
    "Write": "file_path",
    "Edit": "file_path",
    "Read": "file_path",
    "Glob": "pattern",
    "Grep": "pattern",
}

# Which arg holds the body to highlight, paired with its lexer. ``None`` lexer =>
# infer from the headline file extension. Grep/Glob are intentionally absent
# (their pattern already shows in the headline).
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

# Legacy failure heuristic — the fallback for events that predate the structured
# ``success`` field (P0). The projector now reads ``event.success`` when present
# (the executor's ToolResult fact, carried verbatim on PostToolUseEvent) and only
# falls back to sniffing these prefixes when the field is absent. Once the core
# always stamps ``success`` this fallback becomes dead code and can be deleted.
_FAILURE_PREFIXES = ("[PERMISSION DENIED]", "Error", "Traceback", "[PostToolUse]")


# Sentinel distinguishing "event carries no ``success`` field" (legacy → sniff
# prefixes) from a real ``success=False`` fact. ``getattr`` default cannot be a
# plain bool because both True and False are meaningful outcomes.
_NO_SUCCESS_FIELD = object()

# Sentinel distinguishing "event carries no ``media`` field" (legacy → sniff the
# ``Read image`` prefix) from an empty ``media`` list (a real non-media result).
_NO_MEDIA_FIELD = object()


def _judge_failed(event: Any, text: str) -> bool:
    """Did this tool call fail? Read the structured fact; else sniff (legacy).

    The executor already computed success on the ``ToolResult`` and (once the
    core P0 lands) carries it verbatim as ``event.success`` — the honest fact,
    which correctly treats a successful output that happens to start with
    ``Error:`` as a success. When the field is absent (an event built before the
    core change), fall back to the prefix heuristic so behaviour is unchanged.
    """
    success = getattr(event, "success", _NO_SUCCESS_FIELD)
    if success is not _NO_SUCCESS_FIELD:
        return not success
    return text.lstrip().startswith(_FAILURE_PREFIXES)


_MAX_BODY_LINES = 30
_MAX_RESULT_CHARS = 200
_MAX_DETAIL_LINES = 40
# A failed result shows only its first few lines inline (the error headline +
# a little context); the rest folds to a "+N 行已折叠" hint. Errors are terse by
# design, so this is much smaller than the success-path detail budget.
_MAX_FAILURE_LINES = 5
# A plain (non-diff/table) result no longer collapses to its first line: we show
# a preview of up to this many whitespace-separated words so the user gets real
# context before the "… 已折叠" affordance. Original whitespace (newlines, indent)
# is preserved so multi-line command output stays readable.
_MAX_RESULT_WORDS = 100


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


def _fold_lines(text: str, limit: int) -> tuple[str, int]:
    """Keep the first *limit* lines; return ``(kept_text, hidden_count)``.

    The result-detail counterpart to :func:`_truncate_lines`: instead of baking a
    ``"… (N more lines)"`` marker *into* the body (which would then double up with
    the consumer's own fold footnote), it returns the dropped-line **count**
    separately so the projector can ship it as ``ToolCallCompleted.hidden_lines``
    and the consumer renders exactly one clean "+N 行已折叠" hint. ``hidden_count``
    is 0 when nothing was dropped.
    """
    lines = text.splitlines()
    if len(lines) <= limit:
        return text, 0
    return "\n".join(lines[:limit]), len(lines) - limit


def _format_args(args: dict) -> str:
    """Render a tool's args as plain ``key: value`` lines (no JSON braces).

    Scalar values print inline; a multi-line string keeps its key on its own line
    with the body indented; non-scalar values fall back to a compact JSON encoding
    (still far tidier than dumping the whole args dict as pretty-printed JSON).
    """
    lines: List[str] = []
    for key, value in args.items():
        if isinstance(value, str):
            text = value
        else:
            try:
                text = json.dumps(value, ensure_ascii=False, default=str)
            except Exception:  # noqa: BLE001
                text = str(value)
        if "\n" in text:
            body = "\n".join("    " + ln for ln in text.splitlines())
            lines.append(f"{key}:\n{body}")
        else:
            lines.append(f"{key}: {text}")
    return "\n".join(lines)


def _first_nonempty_line(text: str) -> str:
    for line in text.splitlines():
        if line.strip():
            return line.strip()
    return ""


def _preview_words(text: str, limit: int) -> tuple[str, bool]:
    """Return the first *limit* whitespace-separated words, preserving whitespace.

    Walks the string counting word runs; stops after the *limit*-th word and cuts
    at that offset so the kept slice retains its original newlines/indentation
    (unlike ``" ".join(split())`` which would flatten multi-line output onto one
    line). Returns ``(preview, truncated)`` where ``truncated`` is True iff any
    words were dropped. Leading blank lines are stripped so the preview starts on
    real content.
    """
    stripped = text.strip("\n")
    words = 0
    in_word = False
    cut = len(stripped)
    for i, ch in enumerate(stripped):
        if ch.isspace():
            in_word = False
        elif not in_word:
            in_word = True
            words += 1
            if words > limit:
                cut = i
                break
    if words <= limit:
        return stripped, False
    return stripped[:cut].rstrip(), True


def _extract_full_ref(text: str) -> Optional[str]:
    """Pull the persisted full-output path from a framework ``<persisted-output>``.

    The framework's ``tool_result_limit`` replaces an over-large result with a
    ``<persisted-output>`` envelope whose body says ``Full output saved to: <path>``.
    That path is exactly the ViewEvent ``full_ref`` (the complete body a consumer's
    "see full" points at) — semantic truncation, not a physical wire chunk.
    """
    if PERSISTED_OUTPUT_OPEN not in text:
        return None
    marker = "Full output saved to:"
    for line in text.splitlines():
        idx = line.find(marker)
        if idx != -1:
            return line[idx + len(marker) :].strip() or None
    return None


def _looks_like_diff(text: str) -> bool:
    """Heuristic: does this tool output read as a unified diff?

    Conservative — only the two unambiguous shapes: a ``--- / +++`` file-header
    pair, or a ``@@ … @@`` hunk header. This is the one result-classification the
    projector can honestly make *today* from a string payload; media/table kinds
    wait for the framework to surface structured tool results.
    """
    saw_minus_header = False
    for line in text.splitlines():
        if line.startswith("@@") and line.rstrip().endswith("@@"):
            return True
        if line.startswith("--- "):
            saw_minus_header = True
        elif line.startswith("+++ ") and saw_minus_header:
            return True
    return False


class ViewProjector:
    """Folds the ``AgentEvent`` spine into the human ``ViewEvent`` protocol.

    The fold is **stateful only for streaming bookkeeping**: a streamed reply
    arrives as many ``LLMStreamDeltaEvent``\\s followed by one
    ``MessageAppendedEvent`` (role=assistant). The projector opens a block on the
    first delta (so the consumer knows a live region begins), passes deltas
    through, and on the final append emits a ``MessageBlockCompleted`` carrying
    ``streamed=True`` so a streaming consumer *finalizes* its live region rather
    than re-printing. A non-streamed (no-delta) assistant message folds to a
    single ``MessageBlockCompleted(streamed=False)``.
    """

    def __init__(self) -> None:
        # True between the first stream delta and the block's completion. Lets us
        # emit exactly one ``MessageBlockStarted`` and stamp ``streamed`` on the
        # completion correctly.
        self._streaming = False

    # -- the pure fold ------------------------------------------------------

    def project(self, event: Any) -> List[ViewEvent]:
        """Fold one ``AgentEvent`` into zero-or-more ``ViewEvent``\\s (pure)."""
        name = getattr(event, "name", None)

        if name == LLM_STREAM_DELTA:
            token = getattr(event, "token", "") or ""
            if not token:
                return []
            out: List[ViewEvent] = []
            if not self._streaming:
                self._streaming = True
                out.append(MessageBlockStarted(role="assistant"))
            out.append(MessageBlockDelta(text=token))
            return out

        if name == LLM_STREAM_END:
            # The block completion rides on MESSAGE_APPENDED; nothing to emit here
            # (we keep ``_streaming`` set so the append knows it was streamed).
            return []

        if name == MESSAGE_APPENDED:
            return self._project_message(event)

        if name == PRE_TOOL_USE:
            started = self._project_pre_tool(event)
            return [started] if started is not None else []

        if name == POST_TOOL_USE:
            return self._project_post_tool(event)

        if name == TASK_PROGRESS:
            return [
                TaskProgress(
                    stage=getattr(event, "stage", "") or "",
                    status=getattr(event, "status", "") or "",
                    detail=getattr(event, "detail", "") or "",
                )
            ]

        if name == LLM_RETRY:
            # A transient, self-clearing retry countdown (CC's "Retrying in Ns…").
            # Carries the coordinates verbatim; the consumer owns the erasable UI.
            return [
                RetryStatus(
                    attempt=getattr(event, "attempt", 0) or 0,
                    max_attempts=getattr(event, "max_attempts", 0) or 0,
                    delay_ms=getattr(event, "delay_ms", 0.0) or 0.0,
                    error=getattr(event, "error", "") or "",
                    error_type=getattr(event, "error_type", "") or "",
                )
            ]

        if name == COMPACTION_CHECKPOINT:
            # History was compacted (context filled up): the engine rebuilt the
            # transcript, condensing earlier turns into ``summary`` (the new first
            # message). Surface a boundary marker so the human sees why the
            # transcript jumps — claude-code's dim "✻ Conversation compacted".
            messages = getattr(event, "messages", None) or []
            return [
                ConversationCompacted(
                    summary=getattr(event, "summary", "") or "",
                    message_count=len(messages),
                )
            ]

        if name == LLM_ERROR:
            # Silent: per-attempt failures are surfaced transiently via ``RetryStatus``
            # (above), and the *final*, budget-exhausted failure is rendered once via
            # the turn-level ``runtime.last_error → ErrorRaised`` path (driver). Mapping
            # LLM_ERROR here too would spam a red line per attempt + duplicate the final
            # error, which is exactly the CC-divergent noise this change removes.
            return []

        # Everything else (session/turn/file/span/...) is not part of
        # the human view; consumers that want it can subscribe to the spine
        # directly. The projector stays a narrow waist, not a kitchen sink.
        return []

    # -- per-event helpers --------------------------------------------------

    def _project_message(self, event: Any) -> List[ViewEvent]:
        message = getattr(event, "message", None)
        if message is None:
            return []
        role = getattr(message, "role", "") or ""
        # Only the assistant's prose is human-view material here; user echoes and
        # tool-result messages are not re-rendered (the tool panels already show
        # the latter). ``role`` may be a string or a role enum-like — stringify.
        if str(role) != "assistant":
            self._streaming = False
            # One exception: the framework's per-turn ``<system-reminder>`` block
            # (git/token/changed-files/skill/tool/compaction context) is written
            # into history as a user message but is injected context, not a human
            # turn. Surface it as a condensed SystemReminder so the human sees what
            # was fed to the model. The human's own typed prompt is NOT an envelope
            # so it still drops here (the driver renders it separately).
            content = getattr(message, "content", "") or ""
            if _is_system_reminder(content):
                summary = _summarize_reminder(content)
                if summary:
                    return [SystemReminder(text=summary)]
            return []
        content = getattr(message, "content", "") or ""
        streamed = self._streaming
        self._streaming = False
        if not content.strip() and not streamed:
            return []
        return [MessageBlockCompleted(role="assistant", markdown=content, streamed=streamed)]

    def _project_pre_tool(self, event: Any) -> Optional[ToolCallStarted]:
        name = getattr(event, "tool_name", "") or "?"
        args = getattr(event, "tool_input", None) or {}
        tool_use_id = getattr(event, "tool_use_id", None)

        # AskUserQuestion renders via the interactive ask channel; a tool panel
        # would double-print it (matches render.py's skip).
        if name == "AskUserQuestion":
            return None

        headline = ""
        head_arg = _HEADLINE_ARG.get(name)
        if head_arg and isinstance(args.get(head_arg), str):
            headline = args[head_arg]

        body, lexer = self._body_and_lexer(name, args)
        return ToolCallStarted(
            tool_name=name,
            title=name,
            headline=headline,
            body=body,
            lexer=lexer,
            tool_use_id=tool_use_id,
        )

    def _body_and_lexer(self, name: str, args: dict) -> tuple[Optional[str], Optional[str]]:
        """Pick the body text + its lexer once (was ``_body_renderable``)."""
        spec = _BODY.get(name)
        if spec is not None:
            arg_name, lexer = spec
            value = args.get(arg_name)
            if isinstance(value, str) and value.strip():
                if lexer is None:  # infer from the headline file path
                    head_arg = _HEADLINE_ARG.get(name, "")
                    lexer = _lexer_for_path(args.get(head_arg, "") if head_arg else "")
                return _truncate_lines(value, _MAX_BODY_LINES), lexer
            return None, None  # known tool, empty body -> title-only

        # Unknown tool: show args as plain ``key: value`` lines (no JSON braces).
        if args:
            return _truncate_lines(_format_args(args), _MAX_BODY_LINES), None
        return None, None

    def _project_post_tool(self, event: Any) -> List[ViewEvent]:
        """Fold POST_TOOL_USE into a ``ToolCallCompleted`` (+ any media follow-ups).

        Media (images/PDFs a tool produced) rides on the event as structured
        ``ToolMedia`` facts (``event.media``), so we fold one ``MediaBlock`` per
        artifact — image **and** pdf — without sniffing the output text. When the
        field is absent (a legacy event predating the core change) we fall back to
        the old ``"Read image …"`` prefix heuristic so behaviour is unchanged.
        """
        completed = self._complete_tool_event(event)
        out: List[ViewEvent] = [completed]
        out.extend(self._media_blocks(event))
        out.extend(self._file_diff_blocks(event))
        return out

    @staticmethod
    def _file_diff_blocks(event: Any) -> List[FileDiffBlock]:
        """Fold ``event.file_changes`` (structured ``FileChange``) into blocks.

        The change-content counterpart to :meth:`_media_blocks`: one
        ``FileDiffBlock`` per file the tool modified, carrying the ``old``/``new``
        full contents as the fact. A text host synthesizes a coloured diff from
        them; a rich host drives an interactive side-by-side. Empty (or absent)
        for tools that don't modify files — those fall to the ``_looks_like_diff``
        text path in :meth:`_complete_tool_event` instead.
        """
        changes = getattr(event, "file_changes", None) or []
        tool_use_id = getattr(event, "tool_use_id", None)
        blocks: List[FileDiffBlock] = []
        for c in changes:
            raw_path = getattr(c, "path", "") or ""
            path = os.path.abspath(os.path.expanduser(str(raw_path))) if raw_path else ""
            blocks.append(
                FileDiffBlock(
                    path=path,
                    old=getattr(c, "old", "") or "",
                    new=getattr(c, "new", "") or "",
                    tool_use_id=tool_use_id,
                )
            )
        return blocks

    @staticmethod
    def _media_blocks(event: Any) -> List[MediaBlock]:
        """Fold ``event.media`` (structured ``ToolMedia``) into ``MediaBlock``\\s.

        The honest, structured path: the executor mirrored the ToolResult's
        image/pdf artifacts onto the event, each with a local ``ref`` (file path)
        when the tool read from disk. We resolve the path (so a host renders the
        file) and set ``alt`` for text-only degrade. Falls back to the legacy
        prefix sniff only when the field is entirely absent.
        """
        media = getattr(event, "media", _NO_MEDIA_FIELD)
        if media is _NO_MEDIA_FIELD:
            legacy = ViewProjector._legacy_image_block(event)
            return [legacy] if legacy is not None else []
        tool_use_id = getattr(event, "tool_use_id", None)
        blocks: List[MediaBlock] = []
        for m in cast("list[Any]", media) or []:
            kind = getattr(m, "kind", "") or "image"
            raw_ref = getattr(m, "ref", "") or ""
            ref = os.path.abspath(os.path.expanduser(str(raw_ref))) if raw_ref else ""
            alt = (os.path.basename(ref) if ref else "") or kind
            blocks.append(
                MediaBlock(
                    media_kind=kind,
                    ref=ref,
                    mime=getattr(m, "mime", None),
                    alt=alt,
                    tool_use_id=tool_use_id,
                )
            )
        return blocks

    @staticmethod
    def _legacy_image_block(event: Any) -> Optional[MediaBlock]:
        """Pre-``event.media`` fallback: sniff a ``Read``-on-image result's text."""
        if (getattr(event, "tool_name", "") or "") != "Read":
            return None
        response = getattr(event, "tool_response", None)
        text = response if isinstance(response, str) else ("" if response is None else str(response))
        if not text.lstrip().startswith("Read image "):
            return None
        tool_input = getattr(event, "tool_input", None) or {}
        path = tool_input.get("file_path") if isinstance(tool_input, dict) else None
        if not path:
            return None
        resolved = os.path.abspath(os.path.expanduser(str(path)))
        return MediaBlock(
            media_kind="image",
            ref=resolved,
            alt=os.path.basename(resolved) or "image",
            tool_use_id=getattr(event, "tool_use_id", None),
        )

    def _complete_tool_event(self, event: Any) -> ToolCallCompleted:
        """Fold a ``PostToolUse`` into a ``ToolCallCompleted``, dispatched by shape.

        Three honest result shapes, each built by a focused helper: a *failed*
        result (error text folded terse), a *diff*-shaped result (coloured detail),
        and everything else as a word-bounded *plain* preview. A framework-persisted
        result's path is the ``full_ref`` (its presence alone means the content was
        truncated), threaded into whichever branch handles the event.
        """
        name = getattr(event, "tool_name", "") or ""
        tool_use_id = getattr(event, "tool_use_id", None)
        response = getattr(event, "tool_response", None)
        text = response if isinstance(response, str) else ("" if response is None else str(response))
        full_ref = _extract_full_ref(text)

        if _judge_failed(event, text):
            return self._complete_failed(name, tool_use_id, text, full_ref)
        # Prefer a CC-style count summary ("读取 42 行" / "找到 3 个文件") computed
        # once per tool; fall back to the raw first line for tools with no honest
        # count (Bash/terminal/unknown), mirroring claude-code.
        summary = _result_summary(name, event, text) or _first_nonempty_line(text)
        if not summary:
            summary = "(no output)"
        elif len(summary) > _MAX_RESULT_CHARS:
            summary = summary[:_MAX_RESULT_CHARS] + "…"
        if _looks_like_diff(text):
            return self._complete_diff(name, tool_use_id, text, summary, full_ref)
        return self._complete_plain(name, tool_use_id, text, summary, full_ref)

    @staticmethod
    def _complete_failed(
        name: str, tool_use_id: Optional[str], text: str, full_ref: Optional[str]
    ) -> ToolCallCompleted:
        """A failed result: the error text folded to a few lines, char-capped."""
        detail, hidden = _fold_lines(text.strip(), _MAX_FAILURE_LINES)
        truncated = hidden > 0
        if len(detail) > _MAX_RESULT_CHARS:
            detail = detail[:_MAX_RESULT_CHARS] + "…"
            truncated = True
        return ToolCallCompleted(
            tool_name=name,
            ok=False,
            summary=detail,
            tool_use_id=tool_use_id,
            content_truncated=truncated or full_ref is not None,
            full_ref=full_ref,
            hidden_lines=hidden,
        )

    @staticmethod
    def _complete_diff(
        name: str, tool_use_id: Optional[str], text: str, summary: str, full_ref: Optional[str]
    ) -> ToolCallCompleted:
        """A diff-shaped result: ship the body as a ``diff`` detail (+/- colorizable).

        The one result-kind classification honestly available today; all other
        kinds (table/media) await structured framework tool results.
        """
        detail, hidden = _fold_lines(text.strip(), _MAX_DETAIL_LINES)
        return ToolCallCompleted(
            tool_name=name,
            ok=True,
            summary=summary,
            tool_use_id=tool_use_id,
            result_kind=RESULT_KIND_DIFF,
            detail=detail,
            lexer="diff",
            content_truncated=hidden > 0 or full_ref is not None,
            full_ref=full_ref,
            hidden_lines=hidden,
        )

    @staticmethod
    def _complete_plain(
        name: str, tool_use_id: Optional[str], text: str, summary: str, full_ref: Optional[str]
    ) -> ToolCallCompleted:
        """A plain result: a word-bounded preview so the user reads real context.

        Rather than collapse to the one-line summary, ship a preview of up to
        ``_MAX_RESULT_WORDS`` words as the detail body. The preview is truncated
        when words were dropped, the body spilled past the summary, or the
        framework persisted a larger result on disk.
        """
        body = text.strip("\n")
        preview, words_truncated = _preview_words(text, _MAX_RESULT_WORDS)
        # Only carry a detail body when it adds something beyond the summary line
        # (a single-line result already fully shown as the summary needs none).
        detail = preview if preview and preview.strip() != summary else None
        plain_truncated = full_ref is not None or words_truncated or len(body.splitlines()) > 1 or summary.endswith("…")
        # When the word cap dropped whole lines, report how many so the consumer
        # can show a precise "+N 行已折叠" hint (a single very long line clipped at
        # the word boundary hides no *lines*, so this stays 0 and the consumer
        # falls back to a generic fold note).
        hidden = 0
        if words_truncated and preview:
            hidden = max(0, len(body.splitlines()) - len(preview.splitlines()))
        return ToolCallCompleted(
            tool_name=name,
            ok=True,
            summary=summary,
            tool_use_id=tool_use_id,
            result_kind=RESULT_KIND_PLAIN,
            detail=detail,
            content_truncated=plain_truncated,
            full_ref=full_ref,
            hidden_lines=hidden,
        )


__all__ = ["ViewProjector"]
