#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""``TextualSurface`` — land the neutral op stream on the Textual widget tree.

The full-screen host's :class:`RenderSurface`: each op method mutates the
:class:`~mote.product.interfaces.textual.app.MoteApp`'s widget tree with its own
primitives (mount an ``AssistantBlock``, fold into a ``ToolGroupWidget``, flip a
``StatusBar`` reactive, ``remove_children`` on a compaction wipe). The *timing*
decisions (when a group breaks, when a transient clears, when thinking ends) have
already been made by the :class:`TranscriptReducer`; this surface only realises
them as widgets.

Widget **ownership stays on the app** (the surface holds a back-reference and
mutates ``app._open_block`` / ``app._tool_group`` / ``app._tool_widgets`` /
``app._grouped_tool_ids`` / ``app._selected_tool``) so the app's interaction
handlers (``ctrl+o`` fold toggle, click-to-select, compaction clear) — which
produce **no op** because they repaint already-shown rows — keep operating on the
same state the op stream builds. Reusing the app's ``_mount`` / ``_close_block``
helpers keeps the scroll-to-end + block-finalize logic in one place.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from textual.css.query import NoMatches
from textual.widgets import Static

from mote.product.interfaces.textual.widgets import (
    ActivityWidget,
    ApprovalMarkerRow,
    AssistantBlock,
    CompactionSummaryRow,
    ConversationCompactedRow,
    ErrorRow,
    FileDiffRow,
    MediaRow,
    NoticeRow,
    QuestionMarkerRow,
    SessionListWidget,
    StatusBar,
    SystemReminderRow,
    TaskProgressRow,
    ToolCallWidget,
    ToolGroupWidget,
    UserMessageRow,
)
from mote.product.presentation.rich_rendering.builders import (
    RESULT_INDENT,
    FoldMode,
    fold_note,
    indent,
    tool_completed_text,
)
from mote.product.presentation.state import BaseSurface, Truncation


class TextualSurface(BaseSurface):
    """Land each :class:`TranscriptOp` on the :class:`MoteApp`'s widget tree."""

    def __init__(self, app: Any) -> None:
        self._app = app

    # -- block lifecycle --
    def open_block(self, role: str) -> None:
        # A new region opens; the first delta creates the actual widget.
        self._app._close_block()

    def append_delta(self, text: str, reasoning: bool) -> None:
        # Reasoning vs visible text render into the same streaming block; the
        # ``✻ 思考中`` distinction rides on the separate ``set_thinking`` op.
        if not text:
            return
        self._app._ensure_block().append_delta(text)
        self._app._transcript().scroll_end(animate=False)

    def close_block(self, markdown: str, streamed: bool, truncation: Truncation) -> None:
        if streamed:
            # Rendered incrementally already — just finalize if still open. If a
            # tool interleaved it already closed the streamed block (``_open_block``
            # is None), so we must NOT re-render (the old text+tool duplication).
            if self._app._open_block is not None:
                self._app._open_block.finalize()
        elif markdown.strip():
            block = AssistantBlock()
            self._app._mount(block)
            block.set_markdown(markdown)
        self._app._open_block = None
        self._show_truncation(truncation)

    def rollback_block(self) -> None:
        block = self._app._open_block
        self._app._open_block = None
        if block is not None:
            block.remove()

    def render_user_message(self, markdown: str, message_id: Any = None) -> None:
        self._app._close_block()
        if markdown.strip():
            self._app._mount(UserMessageRow(markdown, message_id=message_id))

    # -- standalone tools --
    def tool_started(self, ev: Any, fold: FoldMode) -> None:
        # The reducer already emitted ``flush_group`` for us if a run was open.
        self._app._close_block()
        widget = ToolCallWidget(ev, expanded=self._app._tools_expanded)
        self._app._mount(widget)
        if widget.tool_use_id:
            self._app._tool_widgets[widget.tool_use_id] = widget
            # Keep a reference PAST completion so a structured FileDiffBlock
            # (Edit/Write) folds its diff into this row (see render_file_diff).
            self._app._diff_targets[widget.tool_use_id] = widget
            self._app._media_targets[widget.tool_use_id] = widget

    def tool_completed(self, ev: Any, fold: FoldMode, truncation: Truncation) -> None:
        self._app._close_block()
        tid = getattr(ev, "tool_use_id", None)
        widget = self._app._tool_widgets.pop(tid, None) if tid else None
        if widget is not None:
            # The completion folds into the widget; its body + full output live
            # behind ctrl+o now (every standalone tool folds), so a separate
            # truncation row would dangle below the collapsed row — skip it.
            widget.complete(ev)
            return
        # No matching started widget — render a standalone completed row + note.
        self._app._mount(Static(tool_completed_text(ev)))
        self._show_truncation(truncation)

    # -- collapsed search/read group --
    def open_group(self) -> None:
        self._app._close_block()
        self._app._tool_group = ToolGroupWidget(expanded=self._app._tools_expanded)
        self._app._mount(self._app._tool_group)

    def add_to_group(self, ev: Any) -> None:
        self._app._tool_group.add_started(ev)
        tid = getattr(ev, "tool_use_id", None)
        if tid:
            self._app._grouped_tool_ids[tid] = self._app._tool_group
            self._app._media_targets[tid] = self._app._tool_group

    def complete_in_group(self, ev: Any) -> None:
        tid = getattr(ev, "tool_use_id", None)
        group = self._app._grouped_tool_ids.pop(tid, None) if tid else None
        if group is not None:
            group.complete(ev)

    def flush_group(self) -> None:
        # End the open run; the widget stays mounted (grouped ids survive so a
        # late completion still folds in). Idempotent.
        self._app._tool_group = None

    # -- nested orchestration (run_graph / sub-agent / bg task) --
    def open_activity(self, scope: Any, activity_kind: str, label: str, topology: Any) -> None:
        # One live widget per scope, keyed so scoped pings/child tool calls route
        # to the right subtree even when several activities nest or run in parallel.
        self._app._close_block()
        widget = ActivityWidget(activity_kind, label, topology, expanded=self._app._tools_expanded)
        self._app._mount(widget)
        self._app._activity_widgets[tuple(scope)] = widget

    def update_activity_node(self, scope: Any, stage: str, status: str, detail: str) -> None:
        widget = self._app._activity_widgets.get(tuple(scope))
        if widget is not None:
            widget.update_node(stage, status, detail)

    def add_activity_tool_call(self, scope: Any, ev: Any) -> None:
        widget = self._app._activity_widgets.get(tuple(scope))
        if widget is not None:
            widget.add_child(ev)

    def complete_activity_tool_call(self, scope: Any, ev: Any) -> None:
        widget = self._app._activity_widgets.get(tuple(scope))
        if widget is not None:
            widget.complete_child(ev)

    def close_activity(self, scope: Any, outcome: str, node_states: Any, summary: str) -> None:
        # Freeze the widget to the self-sufficient outcome tree, then drop the
        # live handle (a replayed transcript re-renders straight off node_states).
        widget = self._app._activity_widgets.pop(tuple(scope), None)
        if widget is not None:
            widget.finalize_outcome(outcome, node_states, summary)

    # -- static transcript rows --
    def render_media(self, ev: Any) -> None:
        self._app._close_block()
        media = MediaRow(ev)
        self._app._mount(media)
        tid = getattr(ev, "tool_use_id", None)
        owner = self._app._media_targets.get(tid) if tid else None
        if owner is not None:
            self._app._linked_media.setdefault(owner, []).append(media)
            self._app._media_owners[media] = owner
            media.selected = owner is self._app._selected_tool

    def render_artifact(self, ev: Any) -> None:
        self._app._close_block()
        artifact = ev.artifact
        label = artifact.suggested_name or artifact.kind or artifact.representation
        self._app._mount(Static(f"[artifact] {label}: {artifact.readable}"))

    def render_file_diff(self, ev: Any) -> None:
        # Fold the diff INTO its owning tool row (Edit/Write) so the invocation +
        # change are one select/fold unit; fall back to a standalone FileDiffRow
        # when no tool widget owns it (e.g. an unmatched/idless change).
        tid = getattr(ev, "tool_use_id", None)
        widget = self._app._diff_targets.get(tid) if tid else None
        if widget is not None:
            widget.set_file_diff(ev)
            return
        self._app._close_block()
        self._app._mount(FileDiffRow(ev))

    def render_task_progress(self, ev: Any) -> None:
        if not any((getattr(ev, field, "") or "").strip() for field in ("stage", "status", "detail")):
            return
        self._app._close_block()
        self._app._mount(TaskProgressRow(ev))

    def render_notice(self, ev: Any) -> Any:
        if not (getattr(ev, "text", "") or "").strip():
            return None
        self._app._close_block()
        row = NoticeRow(ev)
        self._app._mount(row)
        return row

    def render_system_reminder(self, ev: Any) -> None:
        if not (getattr(ev, "text", "") or "").strip():
            return
        self._app._close_block()
        self._app._mount(SystemReminderRow(ev))

    def render_error(self, ev: Any) -> None:
        if not (getattr(ev, "text", "") or "").strip():
            return
        self._app._close_block()
        self._app._mount(ErrorRow(ev))

    def render_question(self, ev: Any) -> None:
        if not (getattr(ev, "question", "") or "").strip():
            return
        self._app._close_block()
        self._app._mount(QuestionMarkerRow(ev))

    def render_approval(self, ev: Any) -> None:
        self._app._close_block()
        self._app._mount(ApprovalMarkerRow(ev))

    def render_session_list(self, ev: Any) -> None:
        self._app._close_block()
        self._app._mount(SessionListWidget(ev))

    # -- transient chrome (StatusBar; never a transcript row) --
    def set_thinking(self, on: bool) -> None:
        self._app._set_thinking(on)

    def set_retry(self, ev: Any) -> None:
        self._status_do(lambda bar: bar.set_retry(ev))

    def clear_retry(self) -> None:
        self._status_do(lambda bar: bar.clear_retry())

    def update_usage(self, ev: Any) -> None:
        self._status_do(lambda bar: bar.update_usage(ev))

    def update_runtime_durability(self, ev: Any) -> None:
        self._status_do(lambda bar: bar.update_runtime_durability(ev))

    # -- boundaries / destructive --
    def clear_for_compaction(self, summary: str, message_count: int, last_user_prompt: str) -> None:
        # Alt-buffer host has no scrollback: wipe the now-stale pre-compaction rows
        # and re-render only the bridge — the ✻ boundary, the engine recap, and the
        # last user prompt the post-compaction reply continues to answer.
        self._reset_widgets()
        self._app._last_user_prompt = last_user_prompt
        self._app._transcript().remove_children()
        self._app._mount(ConversationCompactedRow(SimpleNamespace(message_count=message_count)))
        if summary.strip():
            self._app._mount(CompactionSummaryRow(summary))
        if last_user_prompt.strip():
            self._app._mount(UserMessageRow(last_user_prompt))

    def clear_transcript(self) -> None:
        self._reset_widgets()
        self._app._transcript().remove_children()

    # -- helpers --
    def _status_do(self, fn: Any) -> None:
        try:
            fn(self._app.query_one("#status", StatusBar))
        except NoMatches:  # status bar may not be mounted (some test states)
            pass

    def _reset_widgets(self) -> None:
        """Drop the open-block / tool bookkeeping on a screen wipe."""
        self._app._close_block()
        self._app._open_block = None
        self._app._tool_widgets.clear()
        self._app._diff_targets.clear()
        self._app._media_targets.clear()
        self._app._linked_media.clear()
        self._app._media_owners.clear()
        self._app._grouped_tool_ids.clear()
        self._app._tool_group = None
        self._app._activity_widgets.clear()
        self._app._selected_tool = None

    def _show_truncation(self, truncation: Truncation) -> None:
        if not truncation.content_truncated:
            return
        # A FoldMode.DETAIL tool's whole output lives inside its widget behind
        # ctrl+o, so a separate truncation row would dangle — skip it (grouped
        # completions never reach here). ``fold_note`` reads full_ref/hidden_lines
        # straight off the Truncation value object.
        if truncation.fold_mode is FoldMode.DETAIL:
            return
        self._app._mount(Static(indent(fold_note(truncation), RESULT_INDENT)))


__all__ = ["TextualSurface"]
