#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""``MoteApp`` + ``run_textual`` — the full-screen Textual TUI host.

This module owns the asyncio loop for the Textual host. The design (§A–E of the
plan) is:

* **Loop ownership (A)** — the ``App`` runs the event loop; ``SessionDriver.run()``
  runs as a Textual *worker* (``run_worker(..., exclusive=True)`` in ``on_mount``),
  and the app exits when that worker finishes.
* **Input (B)** — a submitted line resolves the :class:`TextualPort`'s pending
  ``read_turn`` Future (or is queued as steering while a turn is in flight).
* **Consumer→widget safety (C)** — the :class:`TextualConsumer` re-posts every
  ``ViewEvent`` as a :class:`ViewEventMessage`; the SINGLE ``on_view_event_message``
  handler below performs all widget mutation on the UI thread, keyed on ``kind``.

``run_textual`` wires the object graph in the one order that resolves the mutual
references (app ⇄ port, app → consumer → app): build the app, the port and the
consumer, hand the consumer to ``build_app`` via ``consumer_objs`` + the port via
``port``, then bind driver+port onto the app and run.
"""

from __future__ import annotations

from typing import Any, Optional

from textual.app import App, ComposeResult
from textual.containers import VerticalScroll
from textual.css.query import NoMatches
from textual.message import Message
from textual.widgets import Static
from textual.worker import Worker, WorkerState

from mote.cli.consumers.render.builders import is_collapsible_tool
from mote.cli.consumers.textual.clipboard import detect_wsl_clipboard, native_copy
from mote.cli.consumers.textual.style import Palette, textual_css_vars
from mote.cli.consumers.textual.widgets import (
    ApprovalMarkerRow,
    AssistantBlock,
    CompactionSummaryRow,
    ConversationCompactedRow,
    ErrorRow,
    FileDiffRow,
    MediaRow,
    NoticeRow,
    PromptInput,
    QuestionMarkerRow,
    SessionListWidget,
    StatusBar,
    SystemReminderRow,
    TaskProgressRow,
    ToolCallWidget,
    ToolGroupWidget,
    UserMessageRow,
)
from mote.cli.contracts.view import (
    APPROVAL_REQUESTED,
    CONVERSATION_COMPACTED,
    ERROR_RAISED,
    FILE_DIFF_BLOCK,
    MEDIA_BLOCK,
    MESSAGE_BLOCK_COMPLETED,
    MESSAGE_BLOCK_DELTA,
    MESSAGE_BLOCK_STARTED,
    NOTICE,
    QUESTION_ASKED,
    REASONING_DELTA,
    RETRY_STATUS,
    SESSION_LIST_SHOWN,
    SYSTEM_REMINDER,
    TASK_PROGRESS,
    TOOL_CALL_COMPLETED,
    TOOL_CALL_STARTED,
    TRANSCRIPT_CLEARED,
    USAGE_UPDATED,
)


class ViewEventMessage(Message):
    """Carries one ``ViewEvent`` from a consumer onto the app's message pump.

    Posting via ``App.post_message`` is thread-safe and FIFO-ordered, so this is
    the single funnel that turns the (possibly off-thread) projected event stream
    into ordered, UI-thread widget mutations (§design C).
    """

    def __init__(self, event: Any) -> None:
        super().__init__()
        self.event = event


class MoteApp(App):
    """The full-screen host: scrolling transcript + status bar + prompt input."""

    CSS = """
    Screen {
        layout: vertical;
    }
    #transcript {
        height: 1fr;
        padding: 0 1;
    }
    #transcript > Static {
        margin-bottom: 1;
    }
    /* Make the mouse text-selection highlight clearly visible against the
       transcript — a full-screen app captures the mouse, so this brand-tinted
       band is the user's feedback that a click-drag actually selected text. */
    .screen--selection {
        background: $brand 40%;
    }
    """

    # Text selection is on by default in this Textual version; state it
    # explicitly so a future widget/theme change can't silently disable the
    # click-drag-to-select the transcript relies on for copy.
    ALLOW_SELECT = True

    BINDINGS = [
        ("ctrl+c", "interrupt", "Interrupt"),
        ("ctrl+d", "quit", "Quit"),
        ("ctrl+o", "toggle_tool_details", "展开/折叠工具"),
    ]

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._session_driver: Any = None
        self._port: Any = None
        self._worker: Optional[Worker] = None
        # The currently-open streaming assistant block (None between blocks).
        self._open_block: Optional[AssistantBlock] = None
        # Tool widgets awaiting their completion event, keyed by tool_use_id.
        self._tool_widgets: dict[str, ToolCallWidget] = {}
        # The open collapsible search/read group (claude-code collapseReadSearch):
        # a run of consecutive Read/Grep/Glob calls coalesces into ONE
        # ``ToolGroupWidget``. ``None`` between runs — any non-transparent event
        # flushes it (see ``on_view_event_message`` / ``_flush_tool_group``).
        self._tool_group: Optional[ToolGroupWidget] = None
        # tool_use_id → the group that owns it, so a completion folds into the
        # right group (distinct from the standalone ``_tool_widgets`` map).
        self._grouped_tool_ids: dict[str, ToolGroupWidget] = {}
        # The global expand/collapse state for tool groups, toggled by ctrl+o.
        # New groups honour it so a mid-session toggle is sticky.
        self._tools_expanded = False
        # The most recent user turn prompt. A full-screen compaction clear wipes
        # the transcript, so we cache it to re-render as the "最近提问" key-info row
        # (the active question the post-compaction reply continues to answer).
        self._last_user_prompt = ""
        # Idle Ctrl+C exit-arm (mirrors the terminal port's armed-flag machine):
        # the first idle press arms + shows a hint, the next *consecutive* press
        # exits the TUI. Cleared when the user submits input, so a later lone
        # press warns afresh instead of exiting.
        self._sigint_armed = False
        # Last completed transcript selection text. A right-click fires
        # ``on_mouse_down`` *after* the drag has ended and the live selection may
        # already be cleared, so we cache the selection as it completes and fall
        # back to it on right-click copy.
        self._last_selection = ""
        # Under WSL we write the Windows clipboard natively (see
        # ``copy_to_clipboard``) instead of emitting OSC 52: VS Code's integrated
        # terminal *appends* an OSC 52 payload to the clipboard rather than
        # replacing it, so repeated copies of the same selection accumulated and
        # looked doubled ("每行复制重复2次"). A native ``Set-Clipboard`` always
        # replaces, so it can never double regardless of the hosting terminal.
        self._wsl_clip = detect_wsl_clipboard()

    # ------------------------------------------------------------------
    # Wiring + theming
    # ------------------------------------------------------------------
    def attach(self, driver: Any, port: Any) -> None:
        """Attach the driver (run as a worker) and the port (input source).

        Named ``attach`` (not ``bind``) because Textual's :class:`App` already owns
        a ``bind`` method for key-binding registration — overriding it would break
        that machinery. Likewise the driver is stored as ``_session_driver`` because
        Textual clobbers ``_driver`` with its own display driver after ``__init__``.
        """
        self._session_driver = driver
        self._port = port

    def get_css_variables(self) -> dict[str, str]:
        base = super().get_css_variables()
        base.update(textual_css_vars())
        return base

    def compose(self) -> ComposeResult:
        yield VerticalScroll(id="transcript")
        yield StatusBar(id="status")
        yield PromptInput(id="prompt")

    def on_mount(self) -> None:
        self.query_one("#prompt", PromptInput).focus()
        if self._session_driver is not None:
            self._worker = self.run_worker(self._session_driver.run(), exclusive=True, exit_on_error=True)

    # ------------------------------------------------------------------
    # Status-bar busy/idle affordance (spinner)
    # ------------------------------------------------------------------
    def set_busy(self) -> None:
        try:
            self.query_one("#status", StatusBar).running = True
        except NoMatches:  # status bar may not be mounted in a test
            pass

    def set_idle(self) -> None:
        try:
            bar = self.query_one("#status", StatusBar)
            bar.running = False
            bar.set_thinking(False)  # a finished turn is no longer reasoning
        except NoMatches:
            pass

    def _set_thinking(self, flag: bool) -> None:
        """Toggle the StatusBar's ``✻ 思考中`` reasoning state (safe if unmounted)."""
        try:
            self.query_one("#status", StatusBar).set_thinking(flag)
        except NoMatches:  # status bar may not be mounted in a test
            pass

    def stage_prompt(self, text: str) -> None:
        try:
            self.query_one("#prompt", PromptInput).value = text
        except NoMatches:
            pass

    # ------------------------------------------------------------------
    # Input → port (feed a turn, or steer while a turn runs)
    # ------------------------------------------------------------------
    def on_input_submitted(self, event: PromptInput.Submitted) -> None:
        # Expand any multi-line paste placeholders back to their real text so the
        # agent receives the full pasted block (see ``PromptInput.consume_value``).
        submitter = event.input
        if isinstance(submitter, PromptInput):
            text = submitter.consume_value()
            images = submitter.consume_images()
        else:
            text, images = event.value, []
        event.input.value = ""
        # Any submitted input disarms a pending Ctrl+C exit — the user is engaged,
        # so a later lone press should warn afresh rather than exit immediately.
        self._sigint_armed = False
        if self._port is None:
            return
        if self._port.is_waiting_for_turn():
            self.set_busy()
            self._port.feed_turn(text, images=images)
        else:
            self._port.submit_steer(None, text)

    def action_interrupt(self) -> None:
        """Ctrl+C: copy a selection if any; else mid-turn interrupt / idle exit.

        **Copy-first.** A full-screen Textual app captures the mouse, so the
        terminal's own click-drag-to-select is unavailable; Textual provides its
        own selection whose default ``ctrl+c → screen.copy_text`` binding our app
        overrode with ``interrupt``. To restore copy we try the selection first:
        if the user has highlighted transcript text, Ctrl+C copies it to the
        clipboard (OSC 52) and returns, leaving the exit-arm untouched.

        With **no** selection it falls through to the terminal-parity armed-flag
        machine (the user chose to keep claude-code's double-press design): during
        a turn Ctrl+C interrupts it and disarms; at an idle prompt the first press
        arms + shows a hint and the next *consecutive* press exits the TUI (via
        ``port.request_exit()`` → the pending ``read_turn`` resolves ``None`` → the
        driver loop ends → the worker finishes → :meth:`on_worker_state_changed`
        calls ``exit()``).
        """
        if self._copy_selection():
            return
        if self._port is None:
            return
        # (no selection to copy — Ctrl+C keeps its interrupt / idle-exit meaning)
        if not self._port.is_waiting_for_turn():
            # Mid-turn: abort the in-flight turn and return to the prompt; disarm
            # any pending idle exit so returning to the prompt starts fresh.
            self._sigint_armed = False
            self._port.signal_interrupt()
        elif self._sigint_armed:
            # Idle prompt, second consecutive press → exit the TUI.
            self._port.request_exit()
        else:
            # Idle prompt, first press → arm + hint; the next press exits.
            self._sigint_armed = True
            self._show_interrupt_hint()

    def _show_interrupt_hint(self) -> None:
        """Mount a dim transcript row prompting a second Ctrl+C to exit."""
        from rich.text import Text

        try:
            self._mount(Static(Text("(Press Ctrl+C again to exit)", style=Palette.DIM)))
        except Exception:  # noqa: BLE001 — transcript may not be mounted in a test
            pass

    def copy_to_clipboard(self, text: str) -> None:
        """Write ``text`` to the clipboard.

        Under WSL we write the Windows clipboard directly via ``Set-Clipboard``
        (which *replaces* atomically) instead of the default OSC 52 escape: VS
        Code's integrated terminal appends OSC 52 payloads rather than replacing,
        so repeated copies looked doubled ("每行复制重复2次"). Elsewhere we keep
        the portable OSC 52 path (``super()``), which also forwards over SSH.
        """
        if self._wsl_clip and native_copy(text):
            return
        super().copy_to_clipboard(text)

    def _current_selection(self) -> str:
        """The live transcript selection text, or ``""`` when nothing is selected.

        ``Screen.get_selected_text()`` returns the highlighted text or ``None``;
        wrapped so a missing/inactive screen (some test states) degrades to ``""``.
        """
        try:
            return self.screen.get_selected_text() or ""
        except Exception:  # noqa: BLE001 — no active screen (e.g. some test states)
            return ""

    def _copy_selection(self) -> bool:
        """Copy the current transcript selection to the clipboard; return whether it did.

        Reproduces Textual's default ``screen.copy_text`` action (which our
        ``ctrl+c`` binding shadowed): copies the live selection via
        ``App.copy_to_clipboard`` (OSC 52). Returns ``False`` (so Ctrl+C keeps
        its interrupt/exit meaning) when there is no selection.
        """
        selected = self._current_selection()
        if not selected:
            return False
        self.copy_to_clipboard(selected)
        return True

    def on_text_selected(self, event: Any) -> None:
        """Cache the selection as a drag completes (Textual posts this on mouse-up).

        A right-click arrives after the drag has ended and Textual may already
        have cleared the live ``screen.selections``; caching here lets
        :meth:`on_mouse_down` copy the just-finished selection.
        """
        text = self._current_selection()
        if text:
            self._last_selection = text

    def on_mouse_down(self, event: Any) -> None:
        """Right-click (button 3) copies the current/last transcript selection.

        A full-screen app captures the mouse, so the hosting terminal's own
        right-click-to-copy is unavailable; this restores it via ``App``'s OSC 52
        clipboard write. Only the right button copies — left/middle are left to
        Textual's normal selection handling.
        """
        if getattr(event, "button", 0) != 3:
            return
        text = self._current_selection() or self._last_selection
        if text:
            self.copy_to_clipboard(text)

    # ------------------------------------------------------------------
    # Worker lifecycle: driver.run() ending → exit the app
    # ------------------------------------------------------------------
    def on_worker_state_changed(self, event: Worker.StateChanged) -> None:
        if event.worker is self._worker and event.state in (
            WorkerState.SUCCESS,
            WorkerState.ERROR,
            WorkerState.CANCELLED,
        ):
            self.exit()

    # ------------------------------------------------------------------
    # The single ViewEvent → widget choke (all mutation on the UI thread)
    # ------------------------------------------------------------------
    def on_view_event_message(self, message: ViewEventMessage) -> None:
        ev = message.event
        kind = getattr(ev, "kind", None)
        # Any event other than a retry-status resolves an in-flight retry (a
        # reply, tool, notice, or the final error), so wipe the transient
        # countdown before rendering it — the retry line never persists.
        if kind != RETRY_STATUS:
            try:
                self.query_one("#status", StatusBar).clear_retry()
            except NoMatches:  # status bar may not be mounted yet
                pass
        # Break the open search/read group on ANY non-transparent event (assistant
        # text, a non-collapsible tool, media, error, …). The grouped tools' own
        # start/complete and status-only events (retry / usage / session-list) pass
        # through so a run keeps coalescing. This is the single UI-thread choke, so
        # the break lives here once rather than in every handler (claude-code
        # breaks the run on the first non-collapsible message it scans).
        if kind not in self._GROUP_TRANSPARENT:
            self._flush_tool_group()
        # Leave the ``✻ 思考中`` state on the first non-reasoning transcript event
        # (a reply delta, a tool, …) — status-only events pass through so a live
        # reasoning stream keeps its thinking label + elapsed timer.
        if kind not in self._THINKING_TRANSPARENT:
            self._set_thinking(False)
        handler = self._DISPATCH.get(kind or "")
        if handler is not None:
            handler(self, ev)

    def _flush_tool_group(self) -> None:
        """End the open search/read group; the widget stays mounted. Idempotent."""
        self._tool_group = None

    # -- transcript mounting helpers --
    def _transcript(self) -> VerticalScroll:
        return self.query_one("#transcript", VerticalScroll)

    def _mount(self, widget: Any) -> None:
        self._transcript().mount(widget)
        self._transcript().scroll_end(animate=False)

    def _close_block(self) -> None:
        """End the open assistant block so the next assistant text starts fresh."""
        if self._open_block is not None:
            self._open_block.finalize()
            self._open_block = None

    def _ensure_block(self) -> AssistantBlock:
        if self._open_block is None:
            self._open_block = AssistantBlock()
            self._mount(self._open_block)
        return self._open_block

    # -- per-kind handlers --
    def _on_block_started(self, ev: Any) -> None:
        self._close_block()  # a new region opens; first delta creates the widget

    def _on_block_delta(self, ev: Any) -> None:
        self._ensure_block().append_delta(getattr(ev, "text", ""))
        self._transcript().scroll_end(animate=False)

    def _on_reasoning_delta(self, ev: Any) -> None:
        # A reasoning (thinking) chunk: flag the ``✻ 思考中`` status state so the
        # bar shows the distinct thinking label + live duration, then render the
        # reasoning text like any other streamed block.
        self._set_thinking(True)
        self._on_block_delta(ev)

    def _on_block_completed(self, ev: Any) -> None:
        if getattr(ev, "role", "assistant") == "user":
            # The user's own turn — mount a ``❯`` prompt row so it stays in the
            # transcript (the PromptInput cleared on submit).
            self._close_block()
            markdown = getattr(ev, "markdown", "")
            if markdown.strip():
                self._last_user_prompt = markdown
                self._mount(UserMessageRow(markdown))
            return
        if getattr(ev, "streamed", False):
            # Already rendered incrementally via the delta stream — just finalize
            # the block if it's still open. If a tool call interleaved in the same
            # turn it already closed (and rendered) the streamed block, so
            # ``_open_block`` is None here and we must NOT re-render (that was the
            # "text + tool call together" duplication).
            if self._open_block is not None:
                self._open_block.finalize()
        elif getattr(ev, "markdown", "").strip():
            block = AssistantBlock()
            self._mount(block)
            block.set_markdown(ev.markdown)
        self._open_block = None
        self._show_truncation(ev)

    def _on_tool_started(self, ev: Any) -> None:
        if is_collapsible_tool(getattr(ev, "tool_name", "")):
            # A search/read call: open a group if none is running, then fold this
            # call in. Consecutive collapsible calls all land in the same widget.
            if self._tool_group is None:
                self._close_block()
                self._tool_group = ToolGroupWidget(expanded=self._tools_expanded)
                self._mount(self._tool_group)
            self._tool_group.add_started(ev)
            tid = getattr(ev, "tool_use_id", None)
            if tid:
                self._grouped_tool_ids[tid] = self._tool_group
            return
        # A non-collapsible tool breaks any open group, then mounts standalone.
        self._flush_tool_group()
        self._close_block()
        widget = ToolCallWidget(ev)
        self._mount(widget)
        if widget.tool_use_id:
            self._tool_widgets[widget.tool_use_id] = widget

    def _on_tool_completed(self, ev: Any) -> None:
        tid = getattr(ev, "tool_use_id", None)
        group = self._grouped_tool_ids.pop(tid, None) if tid else None
        if group is not None:
            # A grouped tool's result folds into its group (the detail lives
            # inside, gated by ctrl+o) — no standalone row / truncation note.
            group.complete(ev)
            return
        self._close_block()
        widget = self._tool_widgets.pop(tid, None) if tid else None
        if widget is not None:
            widget.complete(ev)
        else:  # no matching started widget — render a standalone completed row
            from mote.cli.consumers.render.builders import tool_completed_text

            self._mount(Static(tool_completed_text(ev)))
        self._show_truncation(ev)

    def _on_media(self, ev: Any) -> None:
        self._close_block()
        self._mount(MediaRow(ev))

    def _on_file_diff(self, ev: Any) -> None:
        self._close_block()
        self._mount(FileDiffRow(ev))

    def _on_task_progress(self, ev: Any) -> None:
        self._close_block()
        self._mount(TaskProgressRow(ev))

    def _on_notice(self, ev: Any) -> None:
        self._close_block()
        self._mount(NoticeRow(ev))

    def _on_system_reminder(self, ev: Any) -> None:
        self._close_block()
        self._mount(SystemReminderRow(ev))

    def _on_conversation_compacted(self, ev: Any) -> None:
        # Unlike the terminal host (which keeps history in native scrollback),
        # Textual is an alt-buffer app with NO scrollback — the pre-compaction rows
        # are now stale (the model's context was rebuilt) and can't be scrolled
        # back to anyway. So we mirror claude-code's fullscreen compaction: clear
        # the transcript and re-render only the key info that bridges to what came
        # before — the ✻ boundary, the engine's recap summary, and the last user
        # prompt the post-compaction reply continues to answer.
        self._close_block()
        self._open_block = None
        self._tool_widgets.clear()
        self._grouped_tool_ids.clear()
        self._tool_group = None
        self._transcript().remove_children()
        self._mount(ConversationCompactedRow(ev))
        summary = getattr(ev, "summary", "") or ""
        if summary.strip():
            self._mount(CompactionSummaryRow(summary))
        if self._last_user_prompt.strip():
            self._mount(UserMessageRow(self._last_user_prompt))

    def _on_error(self, ev: Any) -> None:
        self._close_block()
        self._mount(ErrorRow(ev))

    def _on_question(self, ev: Any) -> None:
        self._close_block()
        self._mount(QuestionMarkerRow(ev))

    def _on_approval(self, ev: Any) -> None:
        self._close_block()
        self._mount(ApprovalMarkerRow(ev))

    def _on_usage(self, ev: Any) -> None:
        try:
            self.query_one("#status", StatusBar).update_usage(ev)
        except NoMatches:
            pass

    def _on_retry_status(self, ev: Any) -> None:
        # Transient countdown lives only in the StatusBar — never mount a
        # transcript row (that would persist the retry, contra CC).
        try:
            self.query_one("#status", StatusBar).set_retry(ev)
        except NoMatches:
            pass

    def _on_session_list(self, ev: Any) -> None:
        self._close_block()
        self._mount(SessionListWidget(ev))

    def _on_transcript_cleared(self, ev: Any) -> None:
        # Drop the open streaming block and every mounted transcript row so the
        # human sees a fresh screen (the agent's history was already reset).
        self._open_block = None
        self._tool_widgets.clear()
        self._grouped_tool_ids.clear()
        self._tool_group = None
        self._transcript().remove_children()

    def _show_truncation(self, ev: Any) -> None:
        if not getattr(ev, "content_truncated", False):
            return
        from mote.cli.consumers.render.builders import RESULT_INDENT, fold_note, indent

        self._mount(Static(indent(fold_note(ev), RESULT_INDENT)))

    def action_toggle_tool_details(self) -> None:
        """Ctrl+O: expand/collapse every search/read tool group (claude-code parity).

        Flips the sticky global state and re-renders each mounted group so the
        human can peek at the folded tool detail, then hide it again. New groups
        created afterward honour the current state.
        """
        self._tools_expanded = not self._tools_expanded
        for widget in self.query(ToolGroupWidget):
            widget.set_expanded(self._tools_expanded)

    # Events that DON'T break an open search/read group: the grouped tools' own
    # start/complete, and status-only events that mutate no transcript row.
    _GROUP_TRANSPARENT = frozenset(
        {
            TOOL_CALL_STARTED,
            TOOL_CALL_COMPLETED,
            RETRY_STATUS,
            USAGE_UPDATED,
            SESSION_LIST_SHOWN,
        }
    )

    # Events that DON'T end the ``✻ 思考中`` reasoning state: the reasoning stream
    # itself, and status-only events that mutate no transcript row.
    _THINKING_TRANSPARENT = frozenset(
        {
            REASONING_DELTA,
            USAGE_UPDATED,
            RETRY_STATUS,
        }
    )

    _DISPATCH = {
        MESSAGE_BLOCK_STARTED: _on_block_started,
        MESSAGE_BLOCK_DELTA: _on_block_delta,
        REASONING_DELTA: _on_reasoning_delta,
        MESSAGE_BLOCK_COMPLETED: _on_block_completed,
        TOOL_CALL_STARTED: _on_tool_started,
        TOOL_CALL_COMPLETED: _on_tool_completed,
        MEDIA_BLOCK: _on_media,
        FILE_DIFF_BLOCK: _on_file_diff,
        TASK_PROGRESS: _on_task_progress,
        NOTICE: _on_notice,
        SYSTEM_REMINDER: _on_system_reminder,
        CONVERSATION_COMPACTED: _on_conversation_compacted,
        ERROR_RAISED: _on_error,
        QUESTION_ASKED: _on_question,
        APPROVAL_REQUESTED: _on_approval,
        USAGE_UPDATED: _on_usage,
        SESSION_LIST_SHOWN: _on_session_list,
        RETRY_STATUS: _on_retry_status,
        TRANSCRIPT_CLEARED: _on_transcript_cleared,
    }


def run_textual(
    *,
    model: Optional[str] = None,
    tools: Optional[list] = None,
    cwd: Optional[str] = None,
    name: str = "Assistant",
    config: Any = None,
) -> None:
    """Build the Textual object graph and run the full-screen TUI to completion.

    Wiring order resolves the mutual references: (1) app, (2) port, (3) consumer
    (needs the app), (4) driver via ``build_app(consumer_objs=[consumer], port=port)``,
    (5) bind driver+port onto the app and bind the app onto the port, (6) run.
    """
    from mote.cli.app import build_app
    from mote.cli.consumers.textual.consumer import TextualConsumer
    from mote.common.logs import resume_console_log, suspend_console_log

    app = MoteApp()
    port = TextualPort_lazy()
    consumer = TextualConsumer(app)
    driver = build_app(
        model=model,
        tools=tools,
        cwd=cwd,
        name=name,
        consumer_objs=[consumer],
        port=port,
        config=config,
    )
    app.attach(driver, port)
    port.bind_app(app)
    # A full-screen TUI owns the whole screen, so loguru's live stderr sink would
    # punch through the alternate-screen buffer and reveal log lines behind the UI
    # (on every turn's agent work AND on a Ctrl+C interrupt). Suspend it for the
    # duration — records still reach the file sink — and restore it on exit so the
    # shell that launched us keeps its console logging.
    suspended = suspend_console_log()
    try:
        app.run()
    finally:
        if suspended:
            resume_console_log()


def TextualPort_lazy() -> Any:
    """Late import of :class:`TextualPort` (keeps this module import-light)."""
    from mote.cli.io.textual_io import TextualPort

    return TextualPort()


__all__ = ["MoteApp", "ViewEventMessage", "run_textual"]
