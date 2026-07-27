#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Terminal-only ANSI decoration — the rich-free prompt + banner the port writes.

The neutral colour/glyph tokens live in
:mod:`mote.product.cli.consumers.render.palette` (shared by every host). This module
keeps only the pieces that are specific to the *scrolling terminal* host: a
truecolor ANSI helper and the prompt/banner strings the
:class:`~mote.product.cli.io.terminal_io.TerminalPort` writes directly to a plain
output stream (it never imports ``rich``), so the input line + masthead carry the
same brand orange as the rich consumer without a rendering dependency.
"""

from __future__ import annotations

from mote.product.cli.consumers.render.palette import BULLET, PROMPT_SYMBOL

# Brand accent as an RGB triple — the single numeric source for the ANSI
# (truecolor) prompt/banner below (mirrors ``Palette.BRAND`` = ``#d77757``).
BRAND_RGB = (215, 119, 87)  # brand accent orange
_RESET = "\x1b[0m"


def ansi_fg(text: str, rgb: tuple[int, int, int] = BRAND_RGB, *, bold: bool = False) -> str:
    """Wrap *text* in a truecolor ANSI foreground escape (rich-free).

    Used by the ``TerminalPort`` — which writes raw strings to a plain stream
    and never imports ``rich`` — so the prompt/banner can carry the same brand
    orange as the ``TerminalConsumer`` without a rendering dependency.
    """
    r, g, b = rgb
    lead = "\x1b[1m" if bold else ""
    return f"{lead}\x1b[38;2;{r};{g};{b}m{text}{_RESET}"


# The orange-tinted default prompt the ``TerminalPort`` shows at every read,
# assembled with the rich-free ``ansi_fg`` helper so the input line reads the
# same brand as the consumer's ``❯`` figure.
PROMPT = ansi_fg(PROMPT_SYMBOL, bold=True) + " "


def render_banner() -> str:
    """Return the startup banner (ANSI-coloured, trailing newline).

    A light masthead: the brand-orange bullet + product name
    over a dim hint line. Kept rich-free so the ``TerminalPort`` can print it
    directly to its plain output stream during ``start()``.
    """
    title = ansi_fg(f"{BULLET} Mote CLI", bold=True)
    hint = "\x1b[90m  /help for commands \u00b7 Ctrl-C twice to exit\x1b[0m"
    return f"\n{title}\n{hint}\n\n"


__all__ = [
    "BRAND_RGB",
    "ansi_fg",
    "PROMPT",
    "render_banner",
]
