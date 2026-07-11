#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for :class:`TerminalPort` — stdin reads + two-stage Ctrl+C (§2.5).

The trickiest contracts get the most coverage: the two-stage SIGINT state
machine (mid-turn → interrupt callback; idle → double-press-within-window to
exit) and the single-reader invariant (a parked ``ask`` and the main-loop read
never start two competing ``readline()`` calls). The injection seams
(``get_input_reader`` / ``out`` / ``on_interrupt`` / ``is_turn_running``) let us
drive it deterministically with a fake reader and no real signals/stdin.
"""

from __future__ import annotations

import asyncio
import io

import pytest

from mote.cli.io.terminal_io import TerminalPort
from mote.cli.io.terminal_menu import _menu_lines, _option_lines


class FakeReader:
    """Yields preset lines from ``readline()``; empty bytes == EOF."""

    def __init__(self, lines):
        self._lines = list(lines)

    async def readline(self):
        if self._lines:
            return self._lines.pop(0)
        return b""


class GatedReader:
    """``readline()`` blocks on a queue until a line is fed — for timing tests."""

    def __init__(self):
        self._q: asyncio.Queue = asyncio.Queue()

    async def readline(self):
        return await self._q.get()

    def feed(self, data: bytes) -> None:
        self._q.put_nowait(data)


class KeyReader:
    """Yields preset bytes one keystroke at a time from ``read(n)`` (raw mode)."""

    def __init__(self, chunks):
        # ``chunks`` is an iterable of bytes; each ``read`` pops one chunk so an
        # arrow sequence (b"\x1b", b"[", b"A") is fed as three reads, matching
        # how ``_read_key`` peels a CSI sequence byte-by-byte.
        self._chunks = list(chunks)

    async def read(self, _n=1):
        if self._chunks:
            return self._chunks.pop(0)
        return b""  # EOF


class ApprovalRequest:
    """Minimal stand-in for the gated-action request shape ``decide_approval`` reads."""

    def __init__(self, action="rm -rf /", risk="high", approval_id="abc", args_preview=""):
        self.action = action
        self.risk = risk
        self.approval_id = approval_id
        self.args_preview = args_preview


def make_interactive_port(chunks):
    """A port forced into interactive-select mode with a keystroke reader."""
    buf = io.StringIO()
    port = TerminalPort(get_input_reader=lambda: KeyReader(chunks), out=buf)
    port._reader = KeyReader(chunks)
    port._force_interactive = True
    return port, buf


class MenuReader:
    """Serves raw keystrokes via ``read`` and one line via ``readline``.

    The select menu reads navigation keys with ``read(1)`` (raw mode) and, when
    "Other" is chosen, reads the free-text answer with ``readline`` (cooked mode)
    — so the "select + input" combo exercises both on one reader.
    """

    def __init__(self, chunks, line=b""):
        self._chunks = list(chunks)
        self._line = line

    async def read(self, _n=1):
        return self._chunks.pop(0) if self._chunks else b""

    async def readline(self):
        return self._line


def make_menu_port(reader):
    buf = io.StringIO()
    port = TerminalPort(get_input_reader=lambda: reader, out=buf)
    port._reader = reader
    port._force_interactive = True
    return port, buf


def make_port(lines=None, *, reader=None, **kw):
    buf = io.StringIO()
    rdr = reader if reader is not None else FakeReader(lines or [])
    port = TerminalPort(get_input_reader=lambda: rdr, out=buf, **kw)
    return port, buf


# --------------------------------------------------------------------------
# read_turn
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_read_turn_returns_line():
    port, _buf = make_port([b"hello world\n"])
    await port._setup_stdin()
    line = await port.read_turn()
    assert line == "hello world"


@pytest.mark.asyncio
async def test_read_turn_eof_returns_none_and_sets_exit():
    port, _buf = make_port([b""])  # immediate EOF
    await port._setup_stdin()
    assert await port.read_turn() is None
    assert port.should_exit is True


@pytest.mark.asyncio
async def test_read_turn_restored_bare_enter_resends():
    port, buf = make_port([b"\n"])  # bare Enter
    await port._setup_stdin()
    port.stage_restore("previous prompt")
    line = await port.read_turn()
    assert line == "previous prompt"
    assert "(interrupted" in buf.getvalue()


@pytest.mark.asyncio
async def test_request_exit_short_circuits_read_turn():
    port, _buf = make_port([b"unused\n"])
    await port._setup_stdin()
    port.request_exit()
    assert port.should_exit is True
    assert await port.read_turn() is None


# --------------------------------------------------------------------------
# Two-stage SIGINT
# --------------------------------------------------------------------------


def test_sigint_midturn_invokes_interrupt_callback():
    calls = []
    port, buf = make_port(on_interrupt=lambda: calls.append(1), is_turn_running=lambda: True)
    port._on_sigint()
    assert calls == [1]
    assert "interrupting" in buf.getvalue()


def test_sigint_idle_first_press_warns_no_exit():
    port, buf = make_port(is_turn_running=lambda: False)
    port._on_sigint()
    assert port.should_exit is False
    assert "Ctrl-C again" in buf.getvalue()


def test_sigint_idle_double_press_exits():
    port, _buf = make_port(is_turn_running=lambda: False)
    port._on_sigint()  # arms
    port._on_sigint()  # second consecutive press → exit
    assert port.should_exit is True


@pytest.mark.asyncio
async def test_sigint_idle_second_press_exits_regardless_of_delay():
    """No wall-clock window: a slow second press still exits (the bug fix)."""
    port, _buf = make_port(is_turn_running=lambda: False)
    port._on_sigint()  # arms + warns
    # ...an arbitrarily long human pause would happen here in real use...
    port._on_sigint()  # still exits — arm is not time-bounded
    assert port.should_exit is True


@pytest.mark.asyncio
async def test_submitting_input_resets_sigint_arm():
    """Typing a line between presses disarms, so the next lone press only warns."""
    port, _buf = make_port([b"hello\n"])
    await port._setup_stdin()
    port._on_sigint()  # arm
    assert port._sigint_armed is True
    line = await port.read_turn()  # user submits input → disarm
    assert line == "hello"
    assert port._sigint_armed is False
    port._on_sigint()  # fresh lone press → warns, does NOT exit
    assert port.should_exit is False


# --------------------------------------------------------------------------
# ask — single reader
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ask_direct_reads_line():
    port, buf = make_port([b"my answer\n"])
    await port._setup_stdin()
    ans = await port.ask(None, "What is your name?")
    assert ans == "my answer"
    assert "What is your name?" in buf.getvalue()


@pytest.mark.asyncio
async def test_ask_parked_routes_via_single_reader():
    reader = GatedReader()
    port, _buf = make_port(reader=reader)
    await port._setup_stdin()
    # Main loop parks on a pending read.
    rt = asyncio.ensure_future(port.read_turn())
    await asyncio.sleep(0.01)  # let read_turn create its _read_task
    # A concurrent ask must NOT start a second readline; it parks _ask_waiter.
    ask_fut = asyncio.ensure_future(port.ask(None, "pick one?"))
    await asyncio.sleep(0.01)
    # First line is routed to the parked ask, not consumed as turn input.
    reader.feed(b"the-answer\n")
    ans = await asyncio.wait_for(ask_fut, 1)
    assert ans == "the-answer"
    # The main-loop read resumes and takes the next line as turn input.
    reader.feed(b"turn-input\n")
    text = await asyncio.wait_for(rt, 1)
    assert text == "turn-input"


# --------------------------------------------------------------------------
# ask_questions — structured select + free-text combo (claude-code parity)
# --------------------------------------------------------------------------


def _q(question, header, options, multiSelect=False):
    return {
        "question": question,
        "header": header,
        "options": [{"label": l, "description": d} for l, d in options],
        "multiSelect": multiSelect,
    }


def _questions(*qs):
    from mote.common.schema import AskUserQuestionInput

    return AskUserQuestionInput.model_validate({"questions": list(qs)})


@pytest.mark.asyncio
async def test_ask_questions_enter_picks_first_option():
    # Bare Enter on the highlighted first option → selected=[label], no free text.
    port, buf = make_interactive_port([b"\r"])
    result = await port.ask_questions(None, _questions(_q("Pick a color", "C", [("Red", ""), ("Blue", "")])))
    a = result.answers[0]
    assert a.selected == ["Red"]
    assert a.free_text == ""
    assert "Pick a color" in buf.getvalue()
    assert "Other (type your own answer)" in buf.getvalue()


@pytest.mark.asyncio
async def test_ask_questions_arrow_down_then_enter():
    port, _buf = make_interactive_port([b"\x1b", b"[", b"B", b"\r"])
    result = await port.ask_questions(None, _questions(_q("Pick", "P", [("Red", ""), ("Blue", "")])))
    assert result.answers[0].selected == ["Blue"]


@pytest.mark.asyncio
async def test_ask_questions_digit_shortcut_selects():
    port, _buf = make_interactive_port([b"2"])
    result = await port.ask_questions(None, _questions(_q("Pick", "P", [("Red", ""), ("Blue", ""), ("Green", "")])))
    assert result.answers[0].selected == ["Blue"]


@pytest.mark.asyncio
async def test_ask_questions_other_free_text_verbatim():
    # Navigate to "Other" and type a numeric answer — it stays free text verbatim
    # (the real fix: no digit→index mapping). ``selected`` stays empty.
    reader = MenuReader([b"j", b"j", b"\r"], line=b"42\n")
    port, buf = make_menu_port(reader)
    result = await port.ask_questions(None, _questions(_q("How many?", "Q", [("One", ""), ("Two", "")])))
    a = result.answers[0]
    assert a.selected == []
    assert a.free_text == "42"
    assert "Type your answer:" in buf.getvalue()


@pytest.mark.asyncio
async def test_ask_questions_multi_toggles_and_confirms():
    port, _buf = make_interactive_port([b"1", b"3", b"\r"])
    result = await port.ask_questions(
        None,
        _questions(_q("Toppings", "T", [("Cheese", ""), ("Ham", ""), ("Olives", "")], multiSelect=True)),
    )
    assert result.answers[0].selected == ["Cheese", "Olives"]


@pytest.mark.asyncio
async def test_ask_questions_ctrl_c_returns_empty_answer():
    port, _buf = make_interactive_port([b"\x03"])
    result = await port.ask_questions(None, _questions(_q("Pick", "P", [("Red", ""), ("Blue", "")])))
    a = result.answers[0]
    assert a.selected == []
    assert a.free_text == ""


# --------------------------------------------------------------------------
# Menu chrome — claude-code-aligned brand-orange marker + numbered rows
# --------------------------------------------------------------------------

# Brand orange (``Palette.BRAND`` = #d77757) as a truecolor SGR; the active row
# is painted with it. Bright-black (dim) wraps the inactive numbers/shortcuts.
_ORANGE = "\x1b[38;2;215;119;87m"
_DIM = "\x1b[90m"


def test_select_menu_chrome_marks_active_orange_and_numbers_rows():
    port, _buf = make_interactive_port([])
    entries = ["Red", "Blue", "Other (type your own answer)"]
    lines = _menu_lines(entries, 0, set(), multi=False)
    # Active row (index 0): brand-orange + bold ❯ marker and a visible "1.".
    assert _ORANGE in lines[0] and "\x1b[1m" in lines[0]
    assert "\u276f 1. Red" in lines[0]
    # Inactive rows: dim numbers, no orange, no marker.
    assert f"{_DIM}2.\x1b[0m Blue" in lines[1]
    assert _ORANGE not in lines[1]
    assert "\u276f" not in lines[1]


def test_select_menu_multi_shows_checkboxes_but_not_on_other():
    port, _buf = make_interactive_port([])
    entries = ["Cheese", "Ham", "Other (type your own answer)"]
    lines = _menu_lines(entries, 1, {0}, multi=True)
    assert "[x] Cheese" in lines[0]  # selected
    assert "[ ] Ham" in lines[1]  # active, unselected
    # The trailing "Other" never gets a checkbox (no [x]/[ ] before the label).
    assert "[x]" not in lines[2] and "[ ]" not in lines[2]


def test_approval_menu_chrome_marks_active_orange_dim_shortcuts():
    port, _buf = make_interactive_port([])
    lines = _option_lines(port._APPROVAL_OPTIONS, 0)
    # Active choice: brand-orange bold ❯ + inline shortcut.
    assert _ORANGE in lines[0] and "\u276f Yes (y)" in lines[0]
    # Inactive choices: plain label with a dim ``(shortcut)`` hint.
    assert f"{_DIM}(a)\x1b[0m" in lines[1]
    assert _ORANGE not in lines[1]


@pytest.mark.asyncio
async def test_ask_select_typed_fallback_when_not_interactive():
    # Non-interactive (injected reader, no force) → numbered text list; "2" → label.
    port, buf = make_port([b"2\n"])
    await port._setup_stdin()
    ans = await port.ask(None, "Pick", options=["Red", "Blue"])
    assert ans == "Blue"
    assert "1. Red" in buf.getvalue()
    assert "Other (type your own answer)" in buf.getvalue()


@pytest.mark.asyncio
async def test_ask_select_typed_fallback_free_text():
    port, _buf = make_port([b"custom answer\n"])
    await port._setup_stdin()
    ans = await port.ask(None, "Pick", options=["Red", "Blue"])
    assert ans == "custom answer"


@pytest.mark.asyncio
async def test_ask_without_options_is_plain_text():
    port, buf = make_port([b"my answer\n"])
    await port._setup_stdin()
    ans = await port.ask(None, "What is your name?")
    assert ans == "my answer"


# --------------------------------------------------------------------------
# decide_approval — interactive arrow-key menu (claude-code parity)
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_approval_enter_selects_default_accept():
    # Bare Enter on the first (highlighted) option → accept.
    port, buf = make_interactive_port([b"\r"])
    dec = await port.decide_approval(None, ApprovalRequest())
    assert dec.outcome == "accept"
    assert dec.approval_id == "abc"
    assert "approval required" in buf.getvalue()


@pytest.mark.asyncio
async def test_approval_arrow_down_then_enter_selects_always_allow():
    # ↓ moves to the second option (always_allow), Enter selects it.
    port, _buf = make_interactive_port([b"\x1b", b"[", b"B", b"\r"])
    dec = await port.decide_approval(None, ApprovalRequest())
    assert dec.outcome == "always_allow"


@pytest.mark.asyncio
async def test_approval_shortcut_jumps_and_selects():
    # Pressing "d" jumps straight to always_deny and selects it.
    port, _buf = make_interactive_port([b"d"])
    dec = await port.decide_approval(None, ApprovalRequest())
    assert dec.outcome == "always_deny"


@pytest.mark.asyncio
async def test_approval_esc_rejects():
    # Bare Esc (no following CSI byte) → reject (the safe default).
    port, _buf = make_interactive_port([b"\x1b"])
    dec = await port.decide_approval(None, ApprovalRequest())
    assert dec.outcome == "reject"


@pytest.mark.asyncio
async def test_approval_ctrl_c_rejects():
    port, _buf = make_interactive_port([b"\x03"])
    dec = await port.decide_approval(None, ApprovalRequest())
    assert dec.outcome == "reject"


@pytest.mark.asyncio
async def test_approval_eof_rejects():
    # Empty reader → immediate EOF → reject.
    port, _buf = make_interactive_port([])
    dec = await port.decide_approval(None, ApprovalRequest())
    assert dec.outcome == "reject"


@pytest.mark.asyncio
async def test_approval_up_wraps_to_last_option():
    # ↑ from the top wraps to the last option (always_deny), Enter selects.
    port, _buf = make_interactive_port([b"\x1b", b"[", b"A", b"\r"])
    dec = await port.decide_approval(None, ApprovalRequest())
    assert dec.outcome == "always_deny"


@pytest.mark.asyncio
async def test_approval_jk_navigation():
    # "j" moves down (always_allow), "k" moves back up (accept), Enter selects.
    port, _buf = make_interactive_port([b"j", b"k", b"\r"])
    dec = await port.decide_approval(None, ApprovalRequest())
    assert dec.outcome == "accept"


@pytest.mark.asyncio
async def test_approval_typed_fallback_when_not_interactive():
    # No force flag + injected reader → typed path; "a" → always_allow.
    port, buf = make_port([b"a\n"])
    await port._setup_stdin()
    dec = await port.decide_approval(None, ApprovalRequest())
    assert dec.outcome == "always_allow"
    assert "[y]es" in buf.getvalue()
