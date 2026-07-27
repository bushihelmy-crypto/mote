#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""``ViewProjector`` — the single fold point ``AgentEvent → ViewEvent`` (窄腰).

This is where the old ``render.py``'s per-frontend recompute moves UP and happens
**once**. The projector handles the unified Role telemetry stream (as an
structural telemetry handler) and folds each event into zero-or-more
``ViewEvent``\\s — *deciding once* which tool arg is the headline, which is the
body and its lexer, whether a result failed, and the one-line summary. Those land
on the human protocol as neutral, pre-computed fields; no consumer re-derives them
(ARCHITECTURE §2.2.1 / §4).

``ViewProjector.project`` is the **pure** fold (``AgentEvent → list[ViewEvent]``):
no I/O, no consumer knowledge; trivially unit-testable in isolation. The
**plumbing** that fans this fold out to many consumers (capability downgrade per
consumer, sync/async dispatch) lives in the reusable
:class:`mote.product.cli.contracts.base.BaseProjector`, into which the host injects this
``ViewProjector`` as its concrete fold.
"""

from __future__ import annotations

import json
import os
from typing import Any, List, Optional, cast

from mote.contracts.constants.tool_output import PERSISTED_OUTPUT_OPEN
from mote.contracts.events.types import (
    ACTIVITY_COMPLETED,
    ACTIVITY_STARTED,
    BUDGET,
    CONTEXT_COMPACTED,
    LLM_STREAM_COMMITTED,
    LLM_STREAM_DELTA,
    LLM_STREAM_DISCARDED,
    LLM_STREAM_END,
    LLM_STREAM_INTERRUPTED,
    MESSAGE_APPENDED,
    MODEL_ATTEMPT_FINISHED,
    OUTPUT_COMMITTED,
    OUTPUT_SNAPSHOT,
    OUTPUT_SNAPSHOT_INVALIDATED,
    RUNTIME_DURABILITY_CHANGED,
    TASK_PROGRESS,
    TOOL_CALL_FINISHED,
    TOOL_INVOCATION_STARTED,
)
from mote.product.cli.contracts.view.events import (
    RESULT_KIND_DIFF,
    RESULT_KIND_PLAIN,
    ActivityCompleted,
    ActivityStarted,
    ArtifactBlock,
    AttemptStreamCommitted,
    AttemptStreamDiscarded,
    AttemptStreamInterrupted,
    ConversationCompacted,
    FileDiffBlock,
    MediaBlock,
    MessageBlockCompleted,
    MessageBlockDelta,
    MessageBlockStarted,
    Notice,
    OutputCommitted,
    OutputSnapshot,
    OutputSnapshotInvalidated,
    RuntimeDurabilityStatus,
    SystemReminder,
    TaskProgress,
    ToolCallCompleted,
    ToolCallStarted,
    ViewEvent,
)
from mote.product.cli.view.reminders import _is_system_reminder, _summarize_reminder
from mote.product.cli.view.summaries import _result_summary
from mote.product.i18n import keys as K
from mote.product.i18n import t

# ---------------------------------------------------------------------------
# Tool-formatting tables — ported VERBATIM from ``cli/render.py``.
# These used to be re-applied per frontend; the projector now applies them once
# and ships the result as neutral ``ToolCallStarted`` fields.
# ---------------------------------------------------------------------------

# Which arg holds the "headline" target shown next to the tool name
# (e.g. ``Write  scraper.py``). Absent => no headline (e.g. Bash).
_HEADLINE_ARG = {
    "Edit": "file_path",
    "Read": "file_path",
    "Search": "content",
}


def _search_headline(args: dict) -> str:
    """Headline for a Search call: prefer the content regex, else the files glob."""
    for key in ("content", "files"):
        value = args.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return ""


# Which arg holds the body to highlight, paired with its lexer. ``None`` lexer =>
# infer from the headline file extension. Search is intentionally absent (its
# query already shows in the headline).
_BODY = {
    "Bash": ("command", "bash"),
    "terminal": ("input", "bash"),
    # Edit covers both substring edits and whole-file writes (empty old_string);
    # ``new_string`` carries the changed text / full content in both cases.
    "Edit": ("new_string", None),
    "python": ("code", "python"),
}

# Map a file extension to a Pygments lexer name for Edit content.
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
    """Does this tool *output text* read as a unified diff?

    A text classifier for tools whose **output is itself diff text** — ``git
    diff`` / ``diff`` / ``patch`` — where the payload is a unified diff and there
    is no structured change to carry. Orthogonal to the ``file_changes`` path
    (Edit/Write, which ship the structured ``old``/``new`` change *fact* as a
    ``FileDiffBlock``): the two cover disjoint tools and both are terminal.
    Conservative — only the two unambiguous shapes: a ``--- / +++`` file-header
    pair, or a ``@@ … @@`` hunk header.
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
        """Fold one ``AgentEvent`` into zero-or-more ``ViewEvent``\\s (pure).

        Execution lineage is a *carried-forward* fact, not a per-branch chore:
        after the fold, any non-empty ``scope`` on the source machine event is
        stamped onto every emitted ViewEvent that hasn't set its own. This is the
        narrow-waist — a scoped tool/progress/activity event lands with its
        lineage attached, so the reducer nests it without each helper threading
        scope. Empty scope (the common case) is a no-op → byte-identical output.
        """
        out = self._project(event)
        scope = getattr(event, "scope", ()) or ()
        if scope and out:
            for ev in out:
                if not ev.scope:
                    ev.scope = scope
        return out

    def _project(self, event: Any) -> List[ViewEvent]:
        name = getattr(event, "name", None)

        if name == LLM_STREAM_DELTA:
            token = getattr(event, "token", "") or ""
            if not token:
                return []
            if bool(getattr(event, "provisional", False)):
                return [
                    MessageBlockDelta(
                        text=token,
                        model_call_id=getattr(event, "model_call_id", ""),
                        attempt_id=getattr(event, "attempt_id", ""),
                        sequence=getattr(event, "sequence", 0),
                        provisional=True,
                    )
                ]
            out: List[ViewEvent] = []
            if not self._streaming:
                self._streaming = True
                out.append(MessageBlockStarted(role="assistant"))
            out.append(MessageBlockDelta(text=token))
            return out

        if name == LLM_STREAM_COMMITTED:
            self._streaming = True
            return [
                AttemptStreamCommitted(
                    model_call_id=event.model_call_id,
                    attempt_id=event.attempt_id,
                    chunk_count=event.chunk_count,
                )
            ]

        if name == LLM_STREAM_DISCARDED:
            return [
                AttemptStreamDiscarded(
                    model_call_id=event.model_call_id,
                    attempt_id=event.attempt_id,
                    chunk_count=event.chunk_count,
                    reason=event.reason,
                )
            ]

        if name == LLM_STREAM_INTERRUPTED:
            return [
                AttemptStreamInterrupted(
                    model_call_id=event.model_call_id,
                    attempt_id=event.attempt_id,
                    chunk_count=event.chunk_count,
                    reason=event.reason,
                )
            ]

        if name == LLM_STREAM_END:
            # The block completion rides on MESSAGE_APPENDED; nothing to emit here
            # (we keep ``_streaming`` set so the append knows it was streamed).
            return []

        if name == OUTPUT_SNAPSHOT:
            return [
                OutputSnapshot(
                    run_id=event.run_id,
                    revision=event.revision,
                    schema_fingerprint=event.schema_fingerprint,
                    value=event.value,
                )
            ]

        if name == OUTPUT_SNAPSHOT_INVALIDATED:
            return [
                OutputSnapshotInvalidated(
                    run_id=event.run_id,
                    revision=event.revision,
                    reason=event.reason,
                )
            ]

        if name == OUTPUT_COMMITTED:
            return [
                OutputCommitted(
                    run_id=event.run_id,
                    run_kind=event.run_kind,
                    contract_id=event.contract_id,
                    schema_fingerprint=event.schema_fingerprint,
                    value=event.value,
                )
            ]

        if name == RUNTIME_DURABILITY_CHANGED:
            return [
                RuntimeDurabilityStatus(
                    runtime_id=event.runtime_id,
                    runtime_kind=event.runtime_kind,
                    alias=event.alias,
                    state=event.state,
                    current_revision=event.current_revision,
                    recoverable_revision=event.recoverable_revision,
                    detail=event.detail,
                )
            ]

        if name == MESSAGE_APPENDED:
            return self._project_message(event)

        if name == TOOL_INVOCATION_STARTED:
            started = self._project_tool_started(event)
            return [started] if started is not None else []

        if name == TOOL_CALL_FINISHED:
            return self._project_post_tool(event)

        if name == TASK_PROGRESS:
            return [
                TaskProgress(
                    stage=getattr(event, "stage", "") or "",
                    status=getattr(event, "status", "") or "",
                    detail=getattr(event, "detail", "") or "",
                )
            ]

        if name == ACTIVITY_STARTED:
            return [
                ActivityStarted(
                    activity_kind=getattr(event, "activity_kind", "") or "",
                    label=getattr(event, "label", "") or "",
                    topology=getattr(event, "topology", None),
                )
            ]

        if name == ACTIVITY_COMPLETED:
            return [
                ActivityCompleted(
                    outcome=getattr(event, "outcome", "success") or "success",
                    node_states=list(getattr(event, "node_states", None) or []),
                    summary=getattr(event, "summary", "") or "",
                )
            ]

        if name == BUDGET:
            # A budget threshold was crossed. Both the soft warning and the
            # hard stop reuse the existing Notice row (no dedicated widget);
            # the text distinguishes them and the recorder persists it via the
            # durable observer. Level stays "warning" — Notice has no "error".
            spend = getattr(event, "spend", 0.0) or 0.0
            limit = getattr(event, "limit", 0.0) or 0.0
            stopped = bool(getattr(event, "stopped", False))
            if stopped:
                text = f"Budget cap reached (${spend:.2f} / ${limit:.2f}). Stopping — no further model calls."
            else:
                pct = int((getattr(event, "fraction", 0.0) or 0.0) * 100)
                text = f"Budget warning: {pct}% of cap used (${spend:.2f} / ${limit:.2f})."
            return [Notice(text=text, level="warning")]

        if name == CONTEXT_COMPACTED:
            # History was compacted (context filled up): the engine rebuilt the
            # transcript, condensing earlier turns into ``summary`` (the new first
            # message). Surface a boundary marker so the human sees why the
            # transcript jumps — a dim "✻ Conversation compacted" marker.
            messages = getattr(event, "model_context_messages", None) or []
            return [
                ConversationCompacted(
                    summary=getattr(event, "summary", "") or "",
                    message_count=len(messages),
                )
            ]

        if name == MODEL_ATTEMPT_FINISHED:
            # Attempt details remain on telemetry/tracing. The final exhausted
            # failure is rendered once via runtime.last_error, avoiding one red
            # line per failed credential or endpoint.
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

    def _project_tool_started(self, event: Any) -> Optional[ToolCallStarted]:
        name = getattr(event, "tool_name", "") or "?"
        args = getattr(event, "tool_input", None) or {}
        tool_use_id = getattr(event, "tool_use_id", None)

        # AskUserQuestion renders via the interactive ask channel; a tool panel
        # would double-print it (matches render.py's skip).
        if name == "AskUserQuestion":
            return None

        # RunGraph is an orchestration, not a leaf tool: its detail (the declared
        # topology + per-node progress + outcome) rides on the Activity events
        # (ActivityStarted/Completed) which the reducer nests into a dedicated
        # widget. A title-only row here (no args JSON dump — the same suppression
        # AskUserQuestion gets, but keeping the row so the call is still visible)
        # avoids the raw ``_format_args`` fallback that made RunGraph render as a
        # flat spec blob.
        if name == "RunGraph":
            return ToolCallStarted(
                tool_name=name,
                title=name,
                headline="",
                body=None,
                lexer=None,
                tool_use_id=tool_use_id,
            )

        headline = ""
        if name == "Search":
            # Search has two optional query axes; show whichever is present
            # (content regex preferred, else the files glob).
            headline = _search_headline(args)
        else:
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
        """Fold TOOL_CALL_FINISHED into completion and media view events.

        Media (images/PDFs a tool produced) rides on the event as structured
        ``ToolMedia`` facts (``event.media``), so we fold one ``MediaBlock`` per
        artifact — image **and** pdf — without sniffing the output text.
        """
        completed = self._complete_tool_event(event)
        out: List[ViewEvent] = [completed]
        out.extend(self._media_blocks(event))
        out.extend(self._artifact_blocks(event))
        out.extend(self._file_diff_blocks(event))
        return out

    @staticmethod
    def _artifact_blocks(event: Any) -> List[ArtifactBlock]:
        """Project typed non-media ArtifactRefs without inventing locators."""
        artifacts = getattr(event, "artifacts", None) or []
        tool_use_id = getattr(event, "tool_use_id", None)
        return [ArtifactBlock(artifact=artifact, tool_use_id=tool_use_id) for artifact in artifacts]

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
        file) and set ``alt`` for text-only degrade. Absent/empty ``media`` means
        the result carries no media (an honest empty, not a fallback).
        """
        media = getattr(event, "media", None) or []
        tool_use_id = getattr(event, "tool_use_id", None)
        blocks: List[MediaBlock] = []
        for m in cast("list[Any]", media):
            kind = getattr(m, "kind", "") or "image"
            raw_ref = getattr(m, "ref", "") or ""
            ref = os.path.abspath(os.path.expanduser(str(raw_ref))) if raw_ref else ""
            alt = (os.path.basename(ref) if ref else "") or kind
            blocks.append(
                MediaBlock(
                    media_kind=kind,
                    ref=ref,
                    mime=getattr(m, "mime", None),
                    artifact=getattr(m, "artifact", None),
                    alt=alt,
                    tool_use_id=tool_use_id,
                )
            )
        return blocks

    def _complete_tool_event(self, event: Any) -> ToolCallCompleted:
        """Fold a finished tool call into ``ToolCallCompleted`` by shape.

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

        if getattr(event, "outcome", "succeeded") != "succeeded":
            return self._complete_failed(name, tool_use_id, text, full_ref, getattr(event, "error", None))
        # Prefer a per-tool count summary ("读取 42 行" / "找到 3 个文件") computed
        # once per tool; fall back to the raw first line for tools with no honest
        # count (Bash/terminal/unknown).
        summary = _result_summary(name, event, text) or _first_nonempty_line(text)
        if not summary:
            summary = t(K.RESULT_NO_OUTPUT)
        elif len(summary) > _MAX_RESULT_CHARS:
            summary = summary[:_MAX_RESULT_CHARS] + "…"
        if _looks_like_diff(text):
            return self._complete_diff(name, tool_use_id, text, summary, full_ref)
        return self._complete_plain(name, tool_use_id, text, summary, full_ref)

    @staticmethod
    def _complete_failed(
        name: str,
        tool_use_id: Optional[str],
        text: str,
        full_ref: Optional[str],
        error: Optional[Any],
    ) -> ToolCallCompleted:
        """A failed result: the error text folded to a few lines, char-capped.

        When the executor attached a structured ``ErrorReport`` (``event.error``)
        its code/type/retryable/recovery are read off as flat scalars so a host
        can render machine-reasonable failure facts. ``error is None`` leaves them
        empty — the plain-text summary alone (behaviour equivalent to today).

        The displayed body prefers the report's clean human ``message`` over the
        raw ``<error …>…</error>`` XML block ``render_error_block`` produced for
        the LLM, so the CLI never shows that machine-facing wrapper. Falls back to
        the response text only when no structured report is attached.
        """
        source = (getattr(error, "message", "") or "").strip() or text
        detail, hidden = _fold_lines(source.strip(), _MAX_FAILURE_LINES)
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
            error_type=getattr(error, "error", "") or "",
            error_code=str(getattr(error, "code", "") or ""),
            retryable=bool(getattr(error, "retryable", False)),
            recovery=getattr(error, "recovery", "") or "",
        )

    @staticmethod
    def _complete_diff(
        name: str,
        tool_use_id: Optional[str],
        text: str,
        summary: str,
        full_ref: Optional[str],
    ) -> ToolCallCompleted:
        """A diff-shaped result: ship the body as a ``diff`` detail (+/- colorizable).

        The text-diff terminal path (``git diff`` / ``diff`` output), orthogonal to
        the structured ``file_changes`` → ``FileDiffBlock`` path (Edit/Write): this
        one carries the tool's diff *text*, that one the ``old``/``new`` change fact.
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
        name: str,
        tool_use_id: Optional[str],
        text: str,
        summary: str,
        full_ref: Optional[str],
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
