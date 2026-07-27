#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""``QuestionScreen`` — the Textual select-and-input overlay (§B).

The full-screen analogue of the terminal port's inline select menu: a navigable
:class:`SelectionList` (single- or multi-select) plus an auto "Other" entry that
reveals a free-text :class:`Input`. These tests drive it under an ``App.run_test``
pilot and assert the structured ``(selected, free_text)`` value the screen
dismisses with (captured by the push callback), covering the pick / navigate /
"Other → free text" / multi paths. Free text is kept verbatim — a numeric
"Other" answer stays free text (no digit→label mapping).
"""

from __future__ import annotations

import pytest

pytest.importorskip("textual")

from textual.app import App

from mote.contracts.handoff import DriverHandoffHandle, HandoffRequest, HandoffStatus
from mote.contracts.permissions import ApprovalRequest
from mote.contracts.runtimes import RuntimeRef
from mote.contracts.surfaces import SurfaceDescriptor, SurfacePresentationMode
from mote.product.cli.consumers.textual.screens import ApprovalScreen, HandoffScreen, QuestionScreen
from mote.product.cli.consumers.textual.style import textual_css_vars


class _ScreenHarness(App):
    """Pushes one screen on mount and records its dismissal value."""

    def __init__(self, screen) -> None:
        super().__init__()
        self._screen = screen
        self.result = "__unset__"

    def get_css_variables(self) -> dict:
        base = super().get_css_variables()
        base.update(textual_css_vars())
        return base

    def on_mount(self) -> None:
        self.push_screen(self._screen, self._captured)

    def _captured(self, value) -> None:
        self.result = value


@pytest.mark.asyncio
async def test_single_select_pick_dismisses_with_label():
    app = _ScreenHarness(QuestionScreen("Pick a color", ["Red", "Blue"]))
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("enter")  # toggle the highlighted (first) option
        await pilot.pause()
    assert app.result == (["Red"], "")


@pytest.mark.asyncio
async def test_single_select_navigate_then_pick():
    app = _ScreenHarness(QuestionScreen("Pick", ["Red", "Blue"]))
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("down")  # highlight the second option
        await pilot.press("enter")  # pick it → dismiss
        await pilot.pause()
    assert app.result == (["Blue"], "")


@pytest.mark.asyncio
async def test_other_reveals_free_text_input():
    app = _ScreenHarness(QuestionScreen("Pick", ["Red", "Blue"]))
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("down")  # Blue
        await pilot.press("down")  # Other
        await pilot.press("enter")  # choose Other → reveal + focus the Input
        await pilot.pause()
        for ch in "teal":
            await pilot.press(ch)
        await pilot.press("enter")  # submit the free text
        await pilot.pause()
    assert app.result == ([], "teal")


@pytest.mark.asyncio
async def test_other_numeric_free_text_stays_free_text():
    # Regression #3: a numeric "Other" answer is NOT mapped to an option index.
    app = _ScreenHarness(QuestionScreen("How many?", ["One", "Two"]))
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("down")  # Two
        await pilot.press("down")  # Other
        await pilot.press("enter")  # choose Other → reveal + focus the Input
        await pilot.pause()
        for ch in "42":
            await pilot.press(ch)
        await pilot.press("enter")
        await pilot.pause()
    assert app.result == ([], "42")


@pytest.mark.asyncio
async def test_no_options_is_free_text():
    app = _ScreenHarness(QuestionScreen("Your name?"))
    async with app.run_test() as pilot:
        await pilot.pause()  # on_mount focuses the Input
        for ch in "bob":
            await pilot.press(ch)
        await pilot.press("enter")
        await pilot.pause()
    assert app.result == ([], "bob")


@pytest.mark.asyncio
async def test_multi_select_confirms_via_submit():
    app = _ScreenHarness(QuestionScreen("Toppings", ["Cheese", "Ham", "Olives"], multi=True))
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("enter")  # toggle Cheese (index 0)
        await pilot.press("down")
        await pilot.press("down")  # highlight Olives (index 2)
        await pilot.press("enter")  # toggle Olives
        await pilot.click("#submit")
        await pilot.pause()
    assert app.result == (["Cheese", "Olives"], "")


@pytest.mark.asyncio
async def test_approval_preview_with_percent_encoded_url_is_plain_text():
    request = ApprovalRequest(
        tool_name="Bash",
        target="https://search.jd.com/Search?keyword=%E6%89%8B%E6%9C%BA&sort=sort_totalsales15_desc&page=1",
    )
    app = _ScreenHarness(ApprovalScreen(request))

    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()

    assert app.result.outcome == "reject"


@pytest.mark.asyncio
async def test_handoff_message_waits_for_explicit_complete():
    request = HandoffRequest(runtime_ref=RuntimeRef(runtime_id="r-1", kind="canvas"))
    handle = DriverHandoffHandle(
        handle_id="h-1",
        surface=SurfaceDescriptor(kind="canvas", ref="surface-1", presentation=SurfacePresentationMode.WINDOW),
    )
    app = _ScreenHarness(HandoffScreen(request, handle))

    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.click("#handoff-message")
        for ch in "legend moved":
            await pilot.press(ch)
        await pilot.press("enter")
        await pilot.pause()
        assert app.result == "__unset__"
        await pilot.click("#complete")
        await pilot.pause()

    assert app.result.status is HandoffStatus.COMPLETED
    assert app.result.human_message == "legend moved"


@pytest.mark.asyncio
async def test_handoff_cancel_returns_typed_message():
    request = HandoffRequest(runtime_ref=RuntimeRef(runtime_id="r-2", kind="terminal"))
    handle = DriverHandoffHandle(handle_id="h-2", surface=SurfaceDescriptor(kind="terminal", ref="surface-2"))
    app = _ScreenHarness(HandoffScreen(request, handle))

    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.click("#handoff-message")
        for ch in "cancelled by me":
            await pilot.press(ch)
        await pilot.click("#cancel")
        await pilot.pause()

    assert app.result.status is HandoffStatus.CANCELLED
    assert app.result.human_message == "cancelled by me"


@pytest.mark.asyncio
async def test_canvas_handoff_uses_compact_window_control_modal():
    request = HandoffRequest(runtime_ref=RuntimeRef(runtime_id="r-4", kind="canvas"))
    handle = DriverHandoffHandle(
        handle_id="h-4",
        surface=SurfaceDescriptor(kind="canvas", ref="surface-4", presentation=SurfacePresentationMode.WINDOW),
    )
    app = _ScreenHarness(HandoffScreen(request, handle, window_control=True))

    async with app.run_test() as pilot:
        await pilot.pause()
        assert len(app.query("#canvas-surface-viewport")) == 0
        await pilot.click("#complete")
        await pilot.pause()

    assert app.result.status is HandoffStatus.COMPLETED
