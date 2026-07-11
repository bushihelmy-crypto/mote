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

from mote.cli.consumers.textual.screens import QuestionScreen
from mote.cli.consumers.textual.style import textual_css_vars


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
