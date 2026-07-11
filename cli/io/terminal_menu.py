#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Pure raw-mode menu *chrome* builders shared by :class:`TerminalPort`.

The terminal port drives two navigable raw-mode menus — an ``ask`` select and a
``decide_approval`` prompt — but the *look* of each row (the brand-orange ``❯``
marker, the dim numbers / shortcut hints, the ``[x]``/``[ ]`` checkboxes, the
in-place cursor-up redraw escape) is a pure function of ``(entries, index,
selected, …)`` with no ``self`` and no side effects. Those builders live here so
the port keeps only the stateful concerns (the ``asyncio`` reader + raw termios
state) while the string composition stays independently testable.

The port writes raw strings to a plain stream (never imports ``rich``), so these
builders reuse the rich-free truecolor ``ansi_fg`` (brand orange, mirrors
``Palette.BRAND``) plus a dim (bright-black) wrap for inactive numbers/hints —
matching the consumer's ``❯`` figure so a selection menu reads the same brand as
the rest of the terminal host.
"""

from __future__ import annotations

from typing import List, Sequence, Set

from mote.cli.consumers.render.palette import PROMPT_SYMBOL
from mote.cli.consumers.terminal.style import ansi_fg

_DIM = "\x1b[90m"  # bright-black — dim inactive numbers / shortcut hints
_RESET = "\x1b[0m"

# Warning amber (mirrors ``Palette.WARNING`` = #966c1e) as an RGB triple so the
# approval frame's rule + title carry the same amber the rich consumer uses for
# the gated-action notice — a rich-free tint via ``ansi_fg(rgb=…)``.
_AMBER_RGB = (150, 108, 30)
# Fixed rule width — a light claude-code-style top border for the framed
# approval / ask blocks (the raw stream has no reliable terminal-width probe).
_RULE_WIDTH = 48


def _dim(text: str) -> str:
    """Wrap *text* in a dim (bright-black) ANSI escape (rich-free)."""
    return f"{_DIM}{text}{_RESET}"


def _menu_lines(entries: Sequence[str], index: int, selected: Set[int], multi: bool) -> List[str]:
    """The select-menu block as raw-mode lines (``\\r\\n``), highlighting *index*.

    claude-code look: a numbered list (the visible ``1.``/``2.`` mirror the
    digit shortcuts), the active row marked by a brand-orange ``❯`` + orange
    label, inactive rows dimmed. In multi-select each real option carries a
    ``[x]``/``[ ]`` checkbox (the trailing "Other" row never does).
    """
    other = len(entries) - 1
    lines = []
    for i, label in enumerate(entries):
        box = ("[x] " if i in selected else "[ ] ") if multi and i != other else ""
        num = f"{i + 1}."
        if i == index:
            body = ansi_fg(f"{PROMPT_SYMBOL} {num} {box}{label}", bold=True)
        else:
            body = f"  {_dim(num)} {box}{label}"
        lines.append(f"\r\x1b[2K{body}\r\n")
    return lines


def _redraw_menu(entries: Sequence[str], index: int, selected: Set[int], multi: bool) -> str:
    """Move the cursor back to the block top and repaint the select menu in place."""
    return f"\x1b[{len(entries)}A" + "".join(_menu_lines(entries, index, selected, multi))


def _option_lines(options: Sequence[tuple], index: int) -> List[str]:
    """The approval-option block as raw-mode lines (``\\r\\n``), highlighting *index*.

    claude-code look: the active choice marked by a brand-orange ``❯`` +
    orange label, inactive choices plain with a dim ``(shortcut)`` hint — the
    ``y``/``a``/``n``/``d`` letters are the jump-and-select affordance. *options*
    is a sequence of ``(outcome, label, shortcut)`` triples.
    """
    lines = []
    for i, (_outcome, label, shortcut) in enumerate(options):
        if i == index:
            body = ansi_fg(f"{PROMPT_SYMBOL} {label} ({shortcut})", bold=True)
        else:
            body = f"  {label} {_dim('(' + shortcut + ')')}"
        lines.append(f"\r\x1b[2K{body}\r\n")
    return lines


def _render_option_lines(options: Sequence[tuple], index: int) -> str:
    """First paint of the option block (cursor ends just below it)."""
    return "".join(_option_lines(options, index))


def _redraw_option_lines(options: Sequence[tuple], index: int) -> str:
    """Move the cursor back to the block top and repaint the option block in place."""
    return f"\x1b[{len(options)}A" + "".join(_option_lines(options, index))


__all__ = [
    "_DIM",
    "_RESET",
    "_AMBER_RGB",
    "_RULE_WIDTH",
    "_dim",
    "_menu_lines",
    "_redraw_menu",
    "_option_lines",
    "_render_option_lines",
    "_redraw_option_lines",
]
