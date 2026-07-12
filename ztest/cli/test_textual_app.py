#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""``MoteApp`` — the host wiring under an ``App.run_test`` pilot (§A/B/C).

Covers the three integration seams: (C) a posted ``ViewEventMessage`` mutates the
transcript on the UI thread via the single ``on_view_event_message`` choke; (B) a
submitted prompt line routes to ``port.feed_turn`` (turn) or ``submit_steer``
(mid-turn); (A) the driver worker ending drives ``app.exit()``.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

pytest.importorskip("textual")

from textual.widgets import Static
from textual.worker import WorkerState

from mote.cli.consumers.textual.app import MoteApp, ViewEventMessage
from mote.cli.consumers.textual.widgets import (
    AssistantBlock,
    CompactionSummaryRow,
    ConversationCompactedRow,
    ErrorRow,
    NoticeRow,
    StatusBar,
    ToolCallWidget,
    ToolGroupWidget,
    UserMessageRow,
)
from mote.cli.contracts.view import (
    ConversationCompacted,
    ErrorRaised,
    MessageBlockCompleted,
    MessageBlockDelta,
    Notice,
    ReasoningDelta,
    RetryStatus,
    ToolCallCompleted,
    ToolCallStarted,
)
from mote.common.i18n import keys as K
from mote.common.i18n import t


class _FakePort:
    """Records input routing decisions the app makes on submit/interrupt."""

    def __init__(self, waiting: bool = True) -> None:
        self.waiting = waiting
        self.fed: list = []
        self.fed_images: list = []
        self.steered: list = []
        self.interrupts = 0
        self.exits = 0

    def is_waiting_for_turn(self) -> bool:
        return self.waiting

    def feed_turn(self, text: str, images=None) -> None:
        self.fed.append(text)
        self.fed_images.append(images or [])

    def submit_steer(self, ctx, text: str) -> None:
        self.steered.append(text)

    def signal_interrupt(self) -> None:
        self.interrupts += 1

    def request_exit(self) -> None:
        self.exits += 1


@pytest.mark.asyncio
async def test_delta_event_opens_assistant_block():
    app = MoteApp()
    async with app.run_test() as pilot:
        app.post_message(ViewEventMessage(MessageBlockDelta(text="Hello")))
        await pilot.pause()
        blocks = app.query(AssistantBlock)
        assert len(blocks) == 1
        assert blocks.first()._buf == "Hello"


@pytest.mark.asyncio
async def test_completed_nonstreamed_mounts_fresh_block():
    app = MoteApp()
    async with app.run_test() as pilot:
        app.post_message(ViewEventMessage(MessageBlockCompleted(markdown="# Done", streamed=False)))
        await pilot.pause()
        blocks = app.query(AssistantBlock)
        assert len(blocks) == 1
        assert blocks.first()._buf == "# Done"


@pytest.mark.asyncio
async def test_tool_started_then_completed_correlates_one_widget():
    app = MoteApp()
    async with app.run_test() as pilot:
        # A non-grouping tool (Bash) mounts a standalone ToolCallWidget; a
        # search/read tool would coalesce into a ToolGroupWidget instead.
        app.post_message(ViewEventMessage(ToolCallStarted(tool_name="Bash", tool_use_id="tu-1")))
        await pilot.pause()
        widgets = app.query(ToolCallWidget)
        assert len(widgets) == 1
        assert widgets.first()._completed is None
        app.post_message(ViewEventMessage(ToolCallCompleted(tool_name="Bash", tool_use_id="tu-1", summary="ok")))
        await pilot.pause()
        # Still ONE widget — completion folds into the correlated started widget.
        widgets = app.query(ToolCallWidget)
        assert len(widgets) == 1
        assert widgets.first()._completed is not None
        assert "tu-1" not in app._tool_widgets  # popped after completion


@pytest.mark.asyncio
async def test_consecutive_search_read_coalesce_into_one_group():
    """Read+Grep+Glob in a row → ONE ToolGroupWidget, zero standalone widgets."""
    app = MoteApp()
    async with app.run_test() as pilot:
        app.post_message(ViewEventMessage(ToolCallStarted(tool_name="Read", headline="/a.py", tool_use_id="t1")))
        app.post_message(ViewEventMessage(ToolCallStarted(tool_name="Grep", headline="foo", tool_use_id="t2")))
        app.post_message(ViewEventMessage(ToolCallStarted(tool_name="Glob", headline="*.py", tool_use_id="t3")))
        await pilot.pause()
        assert len(app.query(ToolGroupWidget)) == 1
        assert len(app.query(ToolCallWidget)) == 0
        # Complete them all → the summary reflects the counts.
        app.post_message(ViewEventMessage(ToolCallCompleted(tool_name="Read", tool_use_id="t1", summary="ok")))
        app.post_message(ViewEventMessage(ToolCallCompleted(tool_name="Grep", tool_use_id="t2", summary="ok")))
        app.post_message(ViewEventMessage(ToolCallCompleted(tool_name="Glob", tool_use_id="t3", summary="ok")))
        await pilot.pause()
        group = app.query(ToolGroupWidget).first()
        # ``Static.update`` stashes the raw renderable in the mangled ``__content``;
        # our collapsed group updates it with the summary ``Text``.
        plain = group._Static__content.plain
        assert t(K.GROUP_SEARCH, count=2) in plain
        assert t(K.GROUP_READ, count=1) in plain
        assert "…" not in plain  # all completed → not active


@pytest.mark.asyncio
async def test_noncollapsible_tool_flushes_group_and_mounts_standalone():
    """A Write after a search/read run breaks the group and mounts standalone."""
    app = MoteApp()
    async with app.run_test() as pilot:
        app.post_message(ViewEventMessage(ToolCallStarted(tool_name="Read", headline="/a.py", tool_use_id="t1")))
        await pilot.pause()
        assert len(app.query(ToolGroupWidget)) == 1
        app.post_message(ViewEventMessage(ToolCallStarted(tool_name="Write", headline="/b.py", tool_use_id="t2")))
        await pilot.pause()
        assert app._tool_group is None  # group flushed
        assert len(app.query(ToolGroupWidget)) == 1  # still just the one (unmounted-but-present)
        assert len(app.query(ToolCallWidget)) == 1  # Write mounted standalone


@pytest.mark.asyncio
async def test_assistant_text_breaks_group_so_next_tool_starts_new_group():
    """Assistant text between runs breaks the group; the next tool opens a NEW one."""
    app = MoteApp()
    async with app.run_test() as pilot:
        app.post_message(ViewEventMessage(ToolCallStarted(tool_name="Read", headline="/a.py", tool_use_id="t1")))
        await pilot.pause()
        first = app.query(ToolGroupWidget).first()
        app.post_message(ViewEventMessage(MessageBlockCompleted(markdown="some reply", streamed=False)))
        await pilot.pause()
        assert app._tool_group is None
        app.post_message(ViewEventMessage(ToolCallStarted(tool_name="Grep", headline="foo", tool_use_id="t2")))
        await pilot.pause()
        groups = app.query(ToolGroupWidget)
        assert len(groups) == 2  # a second group started
        assert groups.last() is not first


@pytest.mark.asyncio
async def test_ctrl_o_toggles_tool_group_expansion():
    """``ctrl+o`` flips the global state and honours it for later groups."""
    app = MoteApp()
    async with app.run_test() as pilot:
        app.post_message(ViewEventMessage(ToolCallStarted(tool_name="Read", headline="/a.py", tool_use_id="t1")))
        await pilot.pause()
        group = app.query(ToolGroupWidget).first()
        assert group.expanded is False
        app.action_toggle_tool_details()
        await pilot.pause()
        assert app._tools_expanded is True
        assert group.expanded is True
        # A NEW group created afterward honours the current expanded state.
        app.post_message(ViewEventMessage(MessageBlockCompleted(markdown="reply", streamed=False)))
        app.post_message(ViewEventMessage(ToolCallStarted(tool_name="Grep", headline="foo", tool_use_id="t2")))
        await pilot.pause()
        assert app.query(ToolGroupWidget).last().expanded is True


@pytest.mark.asyncio
async def test_ctrl_o_toggles_standalone_bash_widget():
    """A Bash call mounts collapsed; ctrl+o expands it, and a later Bash call
    honours the now-expanded sticky state."""
    app = MoteApp()
    async with app.run_test() as pilot:
        app.post_message(ViewEventMessage(ToolCallStarted(tool_name="Bash", headline="ls", tool_use_id="b1")))
        await pilot.pause()
        bash = app.query(ToolCallWidget).first()
        assert bash._folds_detail is True and bash.expanded is False
        app.action_toggle_tool_details()
        await pilot.pause()
        assert app._tools_expanded is True and bash.expanded is True
        # A NEW Bash call afterward honours the sticky expanded state.
        app.post_message(ViewEventMessage(ToolCallStarted(tool_name="Bash", headline="pwd", tool_use_id="b2")))
        await pilot.pause()
        assert app.query(ToolCallWidget).last().expanded is True


@pytest.mark.asyncio
async def test_click_selects_tool_row_and_scopes_ctrl_o():
    """Clicking a tool row selects it (distinct band); ctrl+o then toggles ONLY it."""
    from mote.cli.consumers.textual.widgets import FoldableRow

    app = MoteApp()
    async with app.run_test() as pilot:
        # Two standalone Bash rows (both collapsed, detail-folding).
        app.post_message(ViewEventMessage(ToolCallStarted(tool_name="Bash", headline="ls", tool_use_id="b1")))
        app.post_message(ViewEventMessage(MessageBlockCompleted(markdown="x", streamed=False)))
        app.post_message(ViewEventMessage(ToolCallStarted(tool_name="Bash", headline="pwd", tool_use_id="b2")))
        await pilot.pause()
        first, second = app.query(ToolCallWidget)
        # Click (select) the first row.
        app.on_foldable_row_clicked(FoldableRow.Clicked(first))
        await pilot.pause()
        assert app._selected_tool is first
        assert first.selected is True and second.selected is False
        # ctrl+o now scopes to the selected row only.
        app.action_toggle_tool_details()
        await pilot.pause()
        assert first.expanded is True
        assert second.expanded is False  # untouched
        assert app._tools_expanded is False  # global sticky state not flipped


@pytest.mark.asyncio
async def test_click_moves_then_reclick_deselects():
    """Clicking another row moves the selection; re-clicking the selected clears it."""
    from mote.cli.consumers.textual.widgets import FoldableRow

    app = MoteApp()
    async with app.run_test() as pilot:
        app.post_message(ViewEventMessage(ToolCallStarted(tool_name="Bash", headline="ls", tool_use_id="b1")))
        app.post_message(ViewEventMessage(MessageBlockCompleted(markdown="x", streamed=False)))
        app.post_message(ViewEventMessage(ToolCallStarted(tool_name="Bash", headline="pwd", tool_use_id="b2")))
        await pilot.pause()
        first, second = app.query(ToolCallWidget)
        app.on_foldable_row_clicked(FoldableRow.Clicked(first))
        app.on_foldable_row_clicked(FoldableRow.Clicked(second))  # move selection
        await pilot.pause()
        assert app._selected_tool is second
        assert first.selected is False and second.selected is True
        app.on_foldable_row_clicked(FoldableRow.Clicked(second))  # re-click → deselect
        await pilot.pause()
        assert app._selected_tool is None
        assert second.selected is False


@pytest.mark.asyncio
async def test_ctrl_o_stays_global_when_nothing_selected():
    """With no row selected, ctrl+o keeps the global expand/collapse-all behaviour."""
    app = MoteApp()
    async with app.run_test() as pilot:
        app.post_message(ViewEventMessage(ToolCallStarted(tool_name="Bash", headline="ls", tool_use_id="b1")))
        app.post_message(ViewEventMessage(MessageBlockCompleted(markdown="x", streamed=False)))
        app.post_message(ViewEventMessage(ToolCallStarted(tool_name="Bash", headline="pwd", tool_use_id="b2")))
        await pilot.pause()
        first, second = app.query(ToolCallWidget)
        assert app._selected_tool is None
        app.action_toggle_tool_details()
        await pilot.pause()
        assert app._tools_expanded is True
        assert first.expanded is True and second.expanded is True  # all toggled


@pytest.mark.asyncio
async def test_compaction_clears_tool_selection():
    """A compaction wipes the transcript → the dangling selection is released."""
    from mote.cli.consumers.textual.widgets import FoldableRow

    app = MoteApp()
    async with app.run_test() as pilot:
        app.post_message(ViewEventMessage(ToolCallStarted(tool_name="Bash", headline="ls", tool_use_id="b1")))
        await pilot.pause()
        row = app.query(ToolCallWidget).first()
        app.on_foldable_row_clicked(FoldableRow.Clicked(row))
        assert app._selected_tool is row
        app.post_message(ViewEventMessage(ConversationCompacted(summary="", message_count=0)))
        await pilot.pause()
        assert app._selected_tool is None


@pytest.mark.asyncio
async def test_click_selects_whole_search_read_group_and_scopes_ctrl_o():
    """Coalesced Read/Grep/Glob form ONE group → a click selects the whole unit."""
    from mote.cli.consumers.textual.widgets import FoldableRow, ToolGroupWidget

    app = MoteApp()
    async with app.run_test() as pilot:
        # Three consecutive search/read calls coalesce into a single group row.
        app.post_message(ViewEventMessage(ToolCallStarted(tool_name="Read", headline="/a.py", tool_use_id="g1")))
        app.post_message(ViewEventMessage(ToolCallStarted(tool_name="Grep", headline="foo", tool_use_id="g2")))
        app.post_message(ViewEventMessage(ToolCallStarted(tool_name="Glob", headline="*.py", tool_use_id="g3")))
        await pilot.pause()
        groups = app.query(ToolGroupWidget)
        assert len(groups) == 1
        group = groups.first()
        # Clicking the group selects it as one unit.
        app.on_foldable_row_clicked(FoldableRow.Clicked(group))
        await pilot.pause()
        assert app._selected_tool is group and group.selected is True
        assert group.expanded is False
        # Scoped ctrl+o expands the whole group together.
        app.action_toggle_tool_details()
        await pilot.pause()
        assert group.expanded is True


@pytest.mark.asyncio
async def test_notice_and_error_mount_rows():
    app = MoteApp()
    async with app.run_test() as pilot:
        app.post_message(ViewEventMessage(Notice(text="hi", level="info")))
        app.post_message(ViewEventMessage(ErrorRaised(text="boom")))
        await pilot.pause()
        assert len(app.query(NoticeRow)) == 1
        assert len(app.query(ErrorRow)) == 1


@pytest.mark.asyncio
async def test_retry_status_updates_statusbar_not_transcript():
    app = MoteApp()
    async with app.run_test() as pilot:
        app.post_message(
            ViewEventMessage(RetryStatus(attempt=2, max_attempts=6, delay_ms=2000.0, error_type="LLMOverloadedError"))
        )
        await pilot.pause()
        bar = app.query_one("#status", StatusBar)
        assert bar.retry_msg
        assert t(K.RETRY_ATTEMPT, attempt=2, total=6) in bar.render().plain
        # No transcript row was mounted for the transient retry.
        assert len(app.query(NoticeRow)) == 0


@pytest.mark.asyncio
async def test_next_event_clears_retry_statusbar():
    app = MoteApp()
    async with app.run_test() as pilot:
        app.post_message(ViewEventMessage(RetryStatus(attempt=1, max_attempts=6, delay_ms=1000.0)))
        await pilot.pause()
        bar = app.query_one("#status", StatusBar)
        assert bar.retry_msg
        # Any other event (retry resolved) wipes the countdown.
        app.post_message(ViewEventMessage(Notice(text="ok", level="info")))
        await pilot.pause()
        assert bar.retry_msg == ""


@pytest.mark.asyncio
async def test_reasoning_delta_flips_statusbar_to_thinking():
    """A reasoning stream sets the ``✻ 思考中`` thinking flag on the status bar."""
    app = MoteApp()
    async with app.run_test() as pilot:
        app.post_message(ViewEventMessage(ReasoningDelta(text="pondering")))
        await pilot.pause()
        bar = app.query_one("#status", StatusBar)
        assert bar.thinking is True
        assert t(K.STATUS_THINKING) in bar.render().plain


@pytest.mark.asyncio
async def test_non_reasoning_event_clears_thinking():
    """A visible-content event (assistant text) ends the thinking state."""
    app = MoteApp()
    async with app.run_test() as pilot:
        app.post_message(ViewEventMessage(ReasoningDelta(text="pondering")))
        await pilot.pause()
        bar = app.query_one("#status", StatusBar)
        assert bar.thinking is True
        app.post_message(ViewEventMessage(MessageBlockDelta(text="answer")))
        await pilot.pause()
        assert bar.thinking is False


@pytest.mark.asyncio
async def test_submit_while_waiting_feeds_turn():
    app = MoteApp()
    port = _FakePort(waiting=True)
    app.attach(None, port)
    async with app.run_test() as pilot:
        app.query_one("#prompt").focus()
        await pilot.press("h", "i")
        await pilot.press("enter")
        await pilot.pause()
        assert port.fed == ["hi"]
        assert port.steered == []


@pytest.mark.asyncio
async def test_submit_expands_multiline_paste_placeholder():
    """Submitting after a multi-line paste feeds the FULL pasted text to the port.

    End-to-end for "输入框内粘贴的换行内容没有": the visible field holds a one-line
    placeholder, but ``on_input_submitted`` expands it via ``consume_value`` so the
    turn carries the real multi-line content.
    """
    from textual.events import Paste

    from mote.cli.consumers.textual.widgets import PromptInput

    app = MoteApp()
    port = _FakePort(waiting=True)
    app.attach(None, port)
    async with app.run_test() as pilot:
        prompt = app.query_one("#prompt", PromptInput)
        prompt.focus()
        prompt._on_paste(Paste(text="def f():\n    return 1"))
        assert "\n" not in prompt.value  # placeholder only, no raw newline
        await pilot.press("enter")
        await pilot.pause()
        assert port.fed == ["def f():\n    return 1"]


@pytest.mark.asyncio
async def test_submit_mid_turn_steers():
    app = MoteApp()
    port = _FakePort(waiting=False)
    app.attach(None, port)
    async with app.run_test() as pilot:
        app.query_one("#prompt").focus()
        await pilot.press("g", "o")
        await pilot.press("enter")
        await pilot.pause()
        assert port.steered == ["go"]
        assert port.fed == []


@pytest.mark.asyncio
async def test_action_interrupt_midturn_signals_port():
    """Ctrl+C during a turn (not waiting) interrupts and never arms an exit."""
    app = MoteApp()
    port = _FakePort(waiting=False)
    app.attach(None, port)
    async with app.run_test():
        app.action_interrupt()
        assert port.interrupts == 1
        assert port.exits == 0
        assert app._sigint_armed is False


@pytest.mark.asyncio
async def test_ctrl_c_copies_selection_when_present():
    """Ctrl+C with a transcript selection copies it and never interrupts/arms."""
    app = MoteApp()
    port = _FakePort(waiting=True)
    app.attach(None, port)
    async with app.run_test():
        copied: list = []
        app.copy_to_clipboard = lambda text: copied.append(text)  # type: ignore[assignment]
        app.screen.get_selected_text = lambda: "hello world"  # type: ignore[assignment]
        app.action_interrupt()
        assert copied == ["hello world"]  # selection copied to clipboard
        assert port.interrupts == 0  # copy-first short-circuits the interrupt path
        assert port.exits == 0
        assert app._sigint_armed is False  # copying never arms an idle exit


def _mouse_down(button: int):
    """A minimal MouseDown-like event carrying just the pressed ``button``."""
    return SimpleNamespace(button=button)


@pytest.mark.asyncio
async def test_text_selected_caches_selection():
    """``on_text_selected`` caches the completed selection for a later right-click."""
    app = MoteApp()
    async with app.run_test():
        app.screen.get_selected_text = lambda: "drag result"  # type: ignore[assignment]
        app.on_text_selected(SimpleNamespace())
        assert app._last_selection == "drag result"


@pytest.mark.asyncio
async def test_right_click_copies_cached_selection():
    """A right-click with no live selection copies the cached one."""
    app = MoteApp()
    async with app.run_test():
        copied: list = []
        app.copy_to_clipboard = lambda text: copied.append(text)  # type: ignore[assignment]
        app.screen.get_selected_text = lambda: None  # type: ignore[assignment]
        app._last_selection = "cached text"
        app.on_mouse_down(_mouse_down(3))
        assert copied == ["cached text"]


@pytest.mark.asyncio
async def test_right_click_prefers_live_selection():
    """A live selection wins over the cached one on right-click."""
    app = MoteApp()
    async with app.run_test():
        copied: list = []
        app.copy_to_clipboard = lambda text: copied.append(text)  # type: ignore[assignment]
        app.screen.get_selected_text = lambda: "live text"  # type: ignore[assignment]
        app._last_selection = "cached text"
        app.on_mouse_down(_mouse_down(3))
        assert copied == ["live text"]


@pytest.mark.asyncio
async def test_left_click_does_not_copy():
    """A left-click (button 1) never copies — only the right button does."""
    app = MoteApp()
    async with app.run_test():
        copied: list = []
        app.copy_to_clipboard = lambda text: copied.append(text)  # type: ignore[assignment]
        app.screen.get_selected_text = lambda: "some text"  # type: ignore[assignment]
        app._last_selection = "cached text"
        app.on_mouse_down(_mouse_down(1))
        assert copied == []


@pytest.mark.asyncio
async def test_right_click_without_selection_is_noop():
    """A right-click with neither live nor cached selection copies nothing."""
    app = MoteApp()
    async with app.run_test():
        copied: list = []
        app.copy_to_clipboard = lambda text: copied.append(text)  # type: ignore[assignment]
        app.screen.get_selected_text = lambda: None  # type: ignore[assignment]
        app.on_mouse_down(_mouse_down(3))
        assert copied == []


@pytest.mark.asyncio
async def test_wsl_copy_writes_windows_clipboard_natively():
    """Under WSL a copy goes through ``Set-Clipboard`` (native, replaces), NOT OSC 52.

    VS Code's integrated terminal *appends* OSC 52 payloads instead of replacing,
    so repeated copies looked doubled ("每行复制重复2次"); a native replace can't.
    """
    import textual.app as _ta

    from mote.cli.consumers.textual import app as _appmod

    app = MoteApp()
    async with app.run_test():
        osc52: list = []
        native: list = []
        original = _ta.App.copy_to_clipboard
        original_native = _appmod.native_copy
        try:
            _ta.App.copy_to_clipboard = lambda self, text: osc52.append(text)  # type: ignore[assignment]
            app._wsl_clip = True
            _appmod.native_copy = lambda text: (native.append(text) or True)  # type: ignore[assignment]
            app.copy_to_clipboard("● alpha\n● bravo")
            assert native == ["● alpha\n● bravo"]  # written natively
            assert osc52 == []  # OSC 52 base path NOT used → no append/double
        finally:
            _ta.App.copy_to_clipboard = original  # type: ignore[assignment]
            _appmod.native_copy = original_native  # type: ignore[assignment]


@pytest.mark.asyncio
async def test_non_wsl_copy_uses_osc52():
    """Off WSL (or over SSH) the portable OSC 52 base path is used."""
    import textual.app as _ta

    from mote.cli.consumers.textual import app as _appmod

    app = MoteApp()
    async with app.run_test():
        osc52: list = []
        native: list = []
        original = _ta.App.copy_to_clipboard
        original_native = _appmod.native_copy
        try:
            _ta.App.copy_to_clipboard = lambda self, text: osc52.append(text)  # type: ignore[assignment]
            app._wsl_clip = False
            _appmod.native_copy = lambda text: (native.append(text) or True)  # type: ignore[assignment]
            app.copy_to_clipboard("hello")
            assert osc52 == ["hello"]
            assert native == []  # native path skipped when not on WSL

        finally:
            _ta.App.copy_to_clipboard = original  # type: ignore[assignment]
            _appmod.native_copy = original_native  # type: ignore[assignment]


@pytest.mark.asyncio
async def test_wsl_copy_falls_back_to_osc52_when_native_fails():
    """If the native write can't run (no ``powershell.exe``), OSC 52 still copies."""
    import textual.app as _ta

    from mote.cli.consumers.textual import app as _appmod

    app = MoteApp()
    async with app.run_test():
        osc52: list = []
        original = _ta.App.copy_to_clipboard
        original_native = _appmod.native_copy
        try:
            _ta.App.copy_to_clipboard = lambda self, text: osc52.append(text)  # type: ignore[assignment]
            app._wsl_clip = True
            _appmod.native_copy = lambda text: False  # native launch failed  # type: ignore[assignment]
            app.copy_to_clipboard("fallback")
            assert osc52 == ["fallback"]  # degraded to OSC 52
        finally:
            _ta.App.copy_to_clipboard = original  # type: ignore[assignment]
            _appmod.native_copy = original_native  # type: ignore[assignment]


def test_detect_wsl_clipboard_env(monkeypatch):
    """WSL env vars enable native clipboard; SSH disables it (OSC 52 forwards)."""
    from mote.cli.consumers.textual.clipboard import detect_wsl_clipboard

    monkeypatch.delenv("SSH_CONNECTION", raising=False)
    monkeypatch.delenv("SSH_TTY", raising=False)
    monkeypatch.setenv("WSL_DISTRO_NAME", "Ubuntu")
    assert detect_wsl_clipboard() is True
    # An SSH session wins: the copy must reach the operator's terminal via OSC 52.
    monkeypatch.setenv("SSH_CONNECTION", "1.2.3.4 5 6.7.8.9 22")
    assert detect_wsl_clipboard() is False


@pytest.mark.asyncio
async def test_action_interrupt_idle_double_press_exits():
    """Idle Ctrl+C: first press arms + hint, second consecutive press exits."""
    app = MoteApp()
    port = _FakePort(waiting=True)
    app.attach(None, port)
    async with app.run_test() as pilot:
        n_before = len(app.query(Static))
        app.action_interrupt()  # first idle press → arm + mount hint, no exit/interrupt
        await pilot.pause()
        assert app._sigint_armed is True
        assert port.exits == 0
        assert port.interrupts == 0
        assert len(app.query(Static)) == n_before + 1  # a hint row was mounted
        app.action_interrupt()  # second consecutive press → exit
        await pilot.pause()
        assert port.exits == 1


@pytest.mark.asyncio
async def test_submit_disarms_idle_exit():
    """Submitting a line between presses disarms, so a later lone press only arms."""
    app = MoteApp()
    port = _FakePort(waiting=True)
    app.attach(None, port)
    async with app.run_test() as pilot:
        app.action_interrupt()  # arm
        assert app._sigint_armed is True
        app.query_one("#prompt").focus()
        await pilot.press("h", "i")
        await pilot.press("enter")
        await pilot.pause()
        assert app._sigint_armed is False  # submit disarmed
        app.action_interrupt()  # fresh lone press → arms again, does NOT exit
        assert app._sigint_armed is True
        assert port.exits == 0


@pytest.mark.asyncio
async def test_compaction_clears_transcript_and_preserves_key_info():
    """Textual compaction mirrors the fullscreen clear + bridge recap.

    The alt-buffer host has no scrollback, so a ``ConversationCompacted`` boundary
    wipes the now-stale pre-compaction rows and re-renders only the bridge: the ✻
    boundary marker, the engine's recap summary, and the last user prompt the
    post-compaction reply continues to answer.
    """
    app = MoteApp()
    async with app.run_test() as pilot:
        # A prior turn: the human's prompt (cached) + an assistant block + a tool.
        app.post_message(ViewEventMessage(MessageBlockCompleted(markdown="fix the bug", role="user")))
        app.post_message(ViewEventMessage(MessageBlockDelta(text="working on it")))
        app.post_message(ViewEventMessage(ToolCallStarted(tool_name="Bash", tool_use_id="tu-1")))
        await pilot.pause()
        assert len(app.query(AssistantBlock)) == 1
        assert len(app.query(ToolCallWidget)) == 1

        app.post_message(ViewEventMessage(ConversationCompacted(summary="recap of prior work", message_count=5)))
        await pilot.pause()

        # Stale pre-compaction rows are gone.
        assert len(app.query(AssistantBlock)) == 0
        assert len(app.query(ToolCallWidget)) == 0
        # The bridge: exactly one boundary marker + one recap summary.
        assert len(app.query(ConversationCompactedRow)) == 1
        assert len(app.query(CompactionSummaryRow)) == 1
        # The last user prompt (cached on completion) is re-mounted so the
        # reply's context is visible after the clear.
        assert app._last_user_prompt == "fix the bug"
        assert len(app.query(UserMessageRow)) == 1


@pytest.mark.asyncio
async def test_compaction_without_summary_or_prompt_mounts_only_marker():
    """With no recap and no prior prompt, only the ✻ boundary row is re-rendered."""
    app = MoteApp()
    async with app.run_test() as pilot:
        app.post_message(ViewEventMessage(ConversationCompacted(summary="", message_count=0)))
        await pilot.pause()
        assert len(app.query(ConversationCompactedRow)) == 1
        assert len(app.query(CompactionSummaryRow)) == 0
        assert len(app.query(UserMessageRow)) == 0


def test_worker_finished_exits_app():
    app = MoteApp()
    calls: list = []
    app.exit = lambda *a, **k: calls.append(True)  # type: ignore[assignment]
    sentinel = object()
    app._worker = sentinel  # type: ignore[assignment]

    # A terminal state on OUR worker → exit.
    app.on_worker_state_changed(SimpleNamespace(worker=sentinel, state=WorkerState.SUCCESS))
    assert calls == [True]

    # A different worker → ignored.
    app.on_worker_state_changed(SimpleNamespace(worker=object(), state=WorkerState.SUCCESS))
    assert calls == [True]

    # A non-terminal state → ignored.
    app.on_worker_state_changed(SimpleNamespace(worker=sentinel, state=WorkerState.RUNNING))
    assert calls == [True]
