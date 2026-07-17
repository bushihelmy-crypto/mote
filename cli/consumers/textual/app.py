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
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.css.query import NoMatches
from textual.message import Message
from textual.widgets import Static
from textual.worker import Worker, WorkerState

from mote.cli.consumers.textual.clipboard import detect_wsl_clipboard, native_copy
from mote.cli.consumers.textual.style import THEME_NAME, Palette, mote_theme, textual_css_vars
from mote.cli.consumers.textual.surface import TextualSurface
from mote.cli.consumers.textual.widgets import (
    ActivityWidget,
    AssistantBlock,
    FoldableRow,
    PromptInput,
    StatusBar,
    ToolCallWidget,
    ToolGroupWidget,
    UserMessageRow,
)
from mote.cli.consumers.transcript import TranscriptReducer, apply_ops
from mote.cli.contracts.view import Notice
from mote.common.i18n import keys as K
from mote.common.i18n import t


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
        # Localised at import under the default locale (Textual captures binding
        # descriptions statically); the footer label follows the startup language.
        ("ctrl+o", "toggle_tool_details", t(K.KEY_TOGGLE_TOOL)),
        # React-unit delete-mode: ctrl+x arms it (checkboxes appear on user rows),
        # enter confirms the ticked turns, escape cancels. Guarded against an
        # in-flight turn (see ``action_delete_mode``).
        ("ctrl+x", "delete_mode", t(K.KEY_DELETE_MODE)),
        # Confirm/cancel while in delete-mode. Priority bindings so they preempt
        # the focused ``PromptInput`` (whose enter=submit / escape=clear would
        # otherwise swallow them); ``check_action`` disables them when NOT in
        # delete-mode so normal typing keeps enter/escape.
        Binding("enter", "delete_confirm", "Confirm delete", show=False, priority=True),
        Binding("escape", "delete_cancel", "Cancel delete", show=False, priority=True),
    ]

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._session_driver: Any = None
        self._port: Any = None
        self._worker: Optional[Worker] = None
        # The single host-agnostic orchestration machine + this host's surface:
        # ``on_view_event_message`` folds each event through the reducer and lands
        # every resulting op on the surface (which mutates the widgets below).
        self._reducer = TranscriptReducer()
        self._surface = TextualSurface(self)
        # The currently-open streaming assistant block (None between blocks).
        self._open_block: Optional[AssistantBlock] = None
        # Tool widgets awaiting their completion event, keyed by tool_use_id.
        self._tool_widgets: dict[str, ToolCallWidget] = {}
        # The open collapsible search/read group: a run of consecutive
        # Read/Grep/Glob calls coalesces into ONE
        # ``ToolGroupWidget``. ``None`` between runs — the reducer emits a
        # ``flush_group`` op on any non-transparent event (see ``TextualSurface``).
        self._tool_group: Optional[ToolGroupWidget] = None
        # tool_use_id → the group that owns it, so a completion folds into the
        # right group (distinct from the standalone ``_tool_widgets`` map).
        self._grouped_tool_ids: dict[str, ToolGroupWidget] = {}
        # Open nested orchestrations (a run_graph; a sub-agent / bg task) keyed by
        # their ``scope`` (a hashable ScopePath tuple). Each scoped op the reducer
        # emits (per-node progress, a folded child tool call, the close) routes to
        # the keyed :class:`ActivityWidget`; mirrors ``_tool_group``/``_tool_widgets``.
        self._activity_widgets: dict[tuple, ActivityWidget] = {}
        # The global expand/collapse state for tool groups, toggled by ctrl+o.
        # New groups honour it so a mid-session toggle is sticky.
        self._tools_expanded = False
        # The currently click-selected foldable tool row (None = none selected).
        # While set, ctrl+o scopes to JUST this row (peek one call) instead of
        # toggling every row; clicking it again — or a transcript clear — releases
        # it back to the global toggle.
        self._selected_tool: Optional[FoldableRow] = None
        # The most recent user turn prompt. A full-screen compaction clear wipes
        # the transcript, so we cache it to re-render as the "最近提问" key-info row
        # (the active question the post-compaction reply continues to answer).
        self._last_user_prompt = ""
        # React-unit delete-mode: True while the user is ticking turns to prune
        # (ctrl+x arms it, enter confirms, escape cancels). While on, every
        # ``UserMessageRow`` shows a checkbox and enter/escape are captured for
        # confirm/cancel instead of reaching the prompt.
        self._delete_mode = False
        # The transient delete-mode hint banner row, tracked so it can be removed
        # again when the mode is dismissed (Esc leaves no lingering notice).
        self._delete_hint_row: Any = None
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
        # Swap Textual's auto-derived near-black canvas for the ``mote-monokai``
        # theme (Monokai olive-charcoal surfaces + brand-orange accent), so the
        # transcript reads as the familiar cmder/Monokai dark rather than flat black.
        self.register_theme(mote_theme())
        self.theme = THEME_NAME
        self.query_one("#prompt", PromptInput).focus()
        if self._session_driver is not None:
            self._worker = self.run_worker(self._session_driver.run(), exclusive=True, exit_on_error=True)

    # ------------------------------------------------------------------
    # Status-bar busy/idle affordance (spinner)
    # ------------------------------------------------------------------
    def set_busy(self) -> None:
        # A turn beginning cancels any armed delete-mode: pruning history mid-turn
        # would race the running reply, so the ticks are dropped and the user told.
        if self._delete_mode:
            self._set_delete_mode(False)
            self._dismiss_delete_hint()
            self.notice(t(K.DELETE_BUSY), level="warning")
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
        machine (a double-press design): during
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
            self._mount(Static(Text(t(K.KEY_EXIT_HINT), style=Palette.DIM)))
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
        # The single UI-thread choke: fold the event through the host-agnostic
        # reducer (which owns ALL timing — retry clear, group break, thinking
        # end, block/group routing) and land each resulting op on this host's
        # surface via the shared dispatch (same code path the terminal driver runs).
        apply_ops(self._reducer, self._surface, message.event)

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

    def on_foldable_row_clicked(self, message: FoldableRow.Clicked) -> None:
        """A tool row was clicked → make it the single selected row (toggle off if re-clicked).

        Selecting a row scopes the next ``ctrl+o`` to just it; clicking the
        already-selected row clears the selection so ``ctrl+o`` reverts to the
        global expand/collapse. The distinct ``-selected`` background is driven by
        the row's own ``selected`` reactive.
        """
        row = message.row
        if self._selected_tool is row:
            row.selected = False
            self._selected_tool = None
            return
        if self._selected_tool is not None:
            self._selected_tool.selected = False
        self._selected_tool = row
        row.selected = True

    def action_toggle_tool_details(self) -> None:
        """Ctrl+O: expand/collapse folded tool rows.

        With a row **selected** (clicked), scope the toggle to just that row so
        the human peeks at one call's detail. With no selection, flip the sticky
        global state and re-render every mounted :class:`FoldableRow` — search/read
        groups AND detail-folding standalone calls (Bash/Terminal/WebBrowser) alike
        — so new rows created afterward honour the current state.
        """
        if self._selected_tool is not None:
            self._selected_tool.set_expanded(not self._selected_tool.expanded)
            return
        self._tools_expanded = not self._tools_expanded
        for row in self.query(FoldableRow):
            row.set_expanded(self._tools_expanded)

    # ------------------------------------------------------------------
    # React-unit delete-mode (ctrl+x → tick turns → enter confirm / esc cancel)
    # ------------------------------------------------------------------
    def notice(self, text: str, level: str = "info") -> Any:
        """Mount a transient system ``NoticeRow`` (delete-mode feedback etc.).

        Routes through the same surface seam every other row uses so the notice
        lands on the transcript identically to a projected ``Notice`` event.
        Returns the mounted row so a transient hint (e.g. the delete-mode banner)
        can be removed again when the mode is dismissed.
        """
        return self._surface.render_notice(Notice(text=text, level=level))

    def check_action(self, action: str, parameters: tuple) -> bool:
        """Enable the confirm/cancel bindings ONLY while in delete-mode.

        Textual consults this for every binding before firing it; returning
        ``False`` lets the key fall through to the focused widget. So enter/escape
        keep their normal ``PromptInput`` meaning (submit / clear) until delete-mode
        is armed, at which point the priority ``delete_confirm`` / ``delete_cancel``
        bindings take over.
        """
        if action in ("delete_confirm", "delete_cancel"):
            return self._delete_mode
        return True

    def action_delete_mode(self) -> None:
        """Ctrl+X: arm react-unit delete-mode (checkboxes on every user row).

        Blocked while a turn is in flight — pruning history mid-turn would race
        the running reply. A no-op re-press does nothing (already armed).
        """
        if self._delete_mode:
            return
        if self._port is not None and not self._port.is_waiting_for_turn():
            self.notice(t(K.DELETE_BUSY), level="warning")
            return
        self._set_delete_mode(True)
        self._delete_hint_row = self.notice(t(K.DELETE_MODE_HINT))

    def _set_delete_mode(self, on: bool) -> None:
        """Flip delete-mode + push ``select_mode`` onto every mounted user row."""
        self._delete_mode = on
        for row in self.query(UserMessageRow):
            row.select_mode = on

    def action_delete_cancel(self) -> None:
        """Escape (in delete-mode): leave delete-mode, clearing all ticks.

        Cancelling is a no-op on history, so it leaves no trace: the transient
        hint banner is removed rather than replaced with a "cancelled" notice.
        """
        self._set_delete_mode(False)
        self._dismiss_delete_hint()

    def _dismiss_delete_hint(self) -> None:
        """Remove the transient delete-mode hint banner if one is mounted."""
        row = self._delete_hint_row
        self._delete_hint_row = None
        if row is not None:
            try:
                row.remove()
            except Exception:  # noqa: BLE001 — already unmounted is fine
                pass

    def action_delete_confirm(self) -> None:
        """Enter (in delete-mode): prune the ticked react-units, then exit the mode.

        Collects the ``message_id`` of every ticked ``UserMessageRow`` (the delete
        anchors), delegates the durable prune to the driver, and — on success —
        surgically removes the affected rows and their following turn widgets from
        the transcript. An empty selection is a no-op (no checkpoint written).
        """
        # Guard the race where a turn began after delete-mode was armed: never
        # prune history while a reply is in flight.
        if self._port is not None and not self._port.is_waiting_for_turn():
            self._set_delete_mode(False)
            self._dismiss_delete_hint()
            self.notice(t(K.DELETE_BUSY), level="warning")
            return
        ticked = [row for row in self.query(UserMessageRow) if row.selected]
        anchor_ids = [row.message_id for row in ticked if row.message_id]
        if not anchor_ids:
            self._set_delete_mode(False)
            self._dismiss_delete_hint()
            self.notice(t(K.DELETE_NONE), level="warning")
            return
        self.run_worker(self._do_delete(anchor_ids, ticked), exclusive=False)

    async def _do_delete(self, anchor_ids: list, ticked: list) -> None:
        """Run the durable delete, then remove the pruned widgets from the screen."""
        removed = 0
        if self._session_driver is not None:
            removed = await self._session_driver.delete_react_units(anchor_ids)
        self._set_delete_mode(False)
        self._dismiss_delete_hint()
        if removed:
            self._prune_widgets(ticked)
            self._recompute_last_prompt()
            self.notice(t(K.DELETE_DONE, count=removed))
        else:
            self.notice(t(K.DELETE_NONE), level="warning")

    def _prune_widgets(self, anchors: list) -> None:
        """Remove each anchor ``UserMessageRow`` + its following turn's widgets.

        A react-unit's widgets are the anchor row and everything mounted after it
        up to (but excluding) the next ``UserMessageRow`` — mirroring the backend's
        message-level react-unit span. Iterates the live transcript children in
        order so the boundary is read off the actual widget sequence.
        """
        anchor_set = set(anchors)
        children = list(self._transcript().children)
        to_remove: list = []
        i = 0
        n = len(children)
        while i < n:
            widget = children[i]
            if isinstance(widget, UserMessageRow) and widget in anchor_set:
                to_remove.append(widget)
                j = i + 1
                while j < n and not isinstance(children[j], UserMessageRow):
                    to_remove.append(children[j])
                    j += 1
                i = j
            else:
                i += 1
        for widget in to_remove:
            widget.remove()

    def _recompute_last_prompt(self) -> None:
        """Reset ``_last_user_prompt`` to the last surviving user row's text.

        A compaction clear re-renders this cached prompt as the "最近提问" bridge
        row, so after a delete it must track the newest remaining turn (or empty
        when every user row was pruned).
        """
        rows = list(self.query(UserMessageRow))
        self._last_user_prompt = rows[-1]._text if rows else ""


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
