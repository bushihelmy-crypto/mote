#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Modal overlays for interactive input — the Textual analogue of the terminal
port's inline ``ask`` / ``decide_approval`` prompts.

The full-screen TUI can't interleave a blocking ``y/n?`` prompt into the scrolling
transcript the way the raw-stdin terminal port did; instead the
:class:`~mote.cli.io.textual_io.TextualPort` pushes one of these
:class:`~textual.screen.ModalScreen` overlays and awaits its dismissal value:

* :class:`QuestionScreen` — free-form ``ask`` (``AskUserQuestion`` / ``ask_human``);
  dismisses with the answer ``str`` (a bare option number maps to that option's
  label so numbered choices work like the terminal host).
* :class:`ApprovalScreen` — a gated-action permission round-trip; dismisses with an
  :class:`~mote.cli.contracts.view.events.ApprovalDecision` (``y/n/a/d`` keys or
  buttons → ``accept`` / ``reject`` / ``always_allow`` / ``always_deny``).
"""

from __future__ import annotations

from typing import Any, List, Optional

from mote.cli.consumers.textual.style import PROMPT_SYMBOL, WARN
from mote.cli.contracts.view.events import ApprovalDecision
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, SelectionList, Static
from textual.widgets.selection_list import Selection

# Sentinel value for the auto-appended "Other" entry in the selection list.
_OTHER_VALUE = -1


class QuestionScreen(ModalScreen[tuple]):
    """A question overlay combining a navigable selection with free-text input.

    With options it renders a :class:`SelectionList` (keyboard-navigable: ↑/↓ to
    move, Space/Enter to pick) plus an auto "Other (type your own answer)" entry;
    choosing "Other" (or having no options at all) reveals an :class:`Input` for
    free text — selection AND input in one overlay, the Textual analogue of the
    terminal host's inline menu.

    Dismisses with a structured ``(selected: list[str], free_text: str)`` tuple —
    ``(labels, "")`` for a selection, ``([], text)`` for free text. The free text
    is kept verbatim (no digit→label mapping), so a numeric or multi-line answer
    survives intact.
    """

    OTHER_LABEL = "Other (type your own answer)"

    DEFAULT_CSS = """
    QuestionScreen {
        align: center middle;
        background: $surface 40%;
    }
    QuestionScreen > Vertical {
        width: 70%;
        max-width: 100;
        height: auto;
        padding: 1 2;
        border-top: round $question;
        background: $surface;
    }
    QuestionScreen .q-title {
        text-style: bold;
        color: $question;
    }
    QuestionScreen .q-hint {
        color: $dim;
        padding-bottom: 1;
    }
    QuestionScreen SelectionList {
        height: auto;
        max-height: 12;
        background: $surface;
        border: none;
        padding: 0;
    }
    QuestionScreen SelectionList:focus > .selection-list--option-highlighted {
        background: $question 25%;
        color: $text;
        text-style: bold;
    }
    QuestionScreen #answer {
        display: none;
        border: round $question;
        margin-top: 1;
    }
    QuestionScreen #answer.visible {
        display: block;
    }
    QuestionScreen #submit {
        display: none;
        margin-top: 1;
    }
    QuestionScreen #submit.visible {
        display: block;
    }
    QuestionScreen .q-foot {
        color: $dim;
        padding-top: 1;
    }
    """

    def __init__(self, question: str, options: Optional[List[str]] = None, multi: bool = False) -> None:
        super().__init__()
        self._question = question or ""
        self._options = list(options) if options else []
        self._multi = bool(multi)

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label(self._question, classes="q-title")
            if self._options:
                hint = "Space 选择 · Enter 确认" if self._multi else "↑↓ 选择 · Enter 确认"
                yield Label(hint, classes="q-hint")
                selections = [Selection(opt, i) for i, opt in enumerate(self._options)]
                selections.append(Selection(self.OTHER_LABEL, _OTHER_VALUE))
                yield SelectionList(*selections, id="choices")
            yield Input(placeholder="Your answer…", id="answer")
            # Multi-select needs an explicit confirm (single-select dismisses on pick).
            submit = Button("Submit", id="submit", variant="primary")
            if self._options and self._multi:
                submit.add_class("visible")
            yield submit
            yield Label("Esc 取消", classes="q-foot")

    def on_mount(self) -> None:
        if self._options:
            self.query_one("#choices", SelectionList).focus()
        else:
            self._show_input()

    # --- Selection handling --------------------------------------------------

    def on_selection_list_selected_changed(self, event: "SelectionList.SelectedChanged") -> None:
        event.stop()
        choices = event.selection_list
        selected = list(choices.selected)
        if not self._multi and len(selected) > 1:
            # Enforce single-select: keep only the most-recently toggled value.
            keep = selected[-1]
            for value in selected:
                if value != keep:
                    choices.deselect(value)
            selected = [keep]
        if _OTHER_VALUE in selected:
            # "Other" toggles free-text entry rather than being a literal answer.
            self._show_input()
            return
        self._hide_input()
        if not self._multi and selected:
            self.dismiss(([self._options[selected[0]]], ""))  # snappy single-select pick

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        self._confirm()

    def _confirm(self) -> None:
        """Confirm the current selection (multi) or a revealed free-text answer."""
        answer_input = self.query_one("#answer", Input)
        if "visible" in answer_input.classes and answer_input.value.strip():
            self.dismiss(([], answer_input.value.strip()))
            return
        choices = self.query_one("#choices", SelectionList)
        labels = [self._options[v] for v in choices.selected if 0 <= v < len(self._options)]
        self.dismiss((labels, ""))

    # --- Free-text input -----------------------------------------------------

    def _show_input(self) -> None:
        answer_input = self.query_one("#answer", Input)
        answer_input.add_class("visible")
        answer_input.focus()

    def _hide_input(self) -> None:
        self.query_one("#answer", Input).remove_class("visible")

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        # Free text is kept verbatim — no digit→label mapping — so a numeric
        # "Other" answer stays free text (fixes bug #3 in the TUI).
        self.dismiss(([], (event.value or "").strip()))


class ApprovalScreen(ModalScreen[ApprovalDecision]):
    """A gated-action approval overlay; dismisses with an :class:`ApprovalDecision`."""

    DEFAULT_CSS = """
    ApprovalScreen {
        align: center middle;
        background: $surface 40%;
    }
    ApprovalScreen > Vertical {
        width: 70%;
        max-width: 100;
        height: auto;
        padding: 1 2;
        border-top: round $warning;
        background: $surface;
    }
    ApprovalScreen .a-title {
        text-style: bold;
        color: $warning;
    }
    ApprovalScreen .a-risk {
        color: $dim;
    }
    ApprovalScreen .a-action {
        color: $text;
    }
    ApprovalScreen .a-preview {
        color: $dim;
        padding: 1 0 0 0;
    }
    ApprovalScreen .a-proceed {
        color: $text;
        padding: 1 0 0 0;
    }
    ApprovalScreen #buttons {
        height: auto;
        padding-top: 1;
    }
    ApprovalScreen Button {
        width: 100%;
        margin: 0;
        border: none;
        text-align: left;
        background: $surface;
        color: $text;
    }
    ApprovalScreen Button:focus {
        text-style: bold;
        background: $warning 25%;
    }
    ApprovalScreen .a-foot {
        color: $dim;
        padding-top: 1;
    }
    """

    BINDINGS = [
        ("y", "decide('accept')", "Yes"),
        ("n", "decide('reject')", "No"),
        ("a", "decide('always_allow')", "Always"),
        ("d", "decide('always_deny')", "Deny always"),
        ("escape", "decide('reject')", "Cancel"),
    ]

    def __init__(self, request: Any) -> None:
        super().__init__()
        self._action = getattr(request, "action", "") or getattr(request, "tool_name", "") or "action"
        self._risk = getattr(request, "risk", "medium")
        self._preview = getattr(request, "args_preview", "") or ""
        self._approval_id = getattr(request, "approval_id", "") or ""

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label(f"{WARN} approval required", classes="a-title")
            yield Label(f"[{self._risk}]", classes="a-risk")
            yield Label(self._action, classes="a-action")
            if self._preview:
                yield Static(self._preview, classes="a-preview")
            yield Label("Do you want to proceed?", classes="a-proceed")
            with Vertical(id="buttons"):
                yield Button(f"{PROMPT_SYMBOL} 1. Yes (y)", id="accept", variant="success")
                yield Button(
                    f"{PROMPT_SYMBOL} 2. Yes, and don\u2019t ask again (a)", id="always_allow", variant="primary"
                )
                yield Button(f"{PROMPT_SYMBOL} 3. No, tell me what to do (n · esc)", id="reject", variant="error")
                yield Button(f"{PROMPT_SYMBOL} 4. No, never allow this (d)", id="always_deny", variant="warning")
            yield Label("↑↓ 选择 · Enter 确认 · Esc 取消", classes="a-foot")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.action_decide(event.button.id or "reject")

    def action_decide(self, outcome: str) -> None:
        self.dismiss(ApprovalDecision(approval_id=self._approval_id, outcome=outcome))


__all__ = ["QuestionScreen", "ApprovalScreen"]
