#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Textual CSS variables derived from the single :class:`Palette`.

The Textual TUI must read the **same** colour tokens as the rich terminal so the
two hosts never diverge on brand orange / success green / diff colours. Rather
than a second colour source, :func:`textual_css_vars` projects the one neutral
:class:`~mote.cli.consumers.render.palette.Palette` (hex tokens) into Textual's
``$variable`` map, which :class:`~mote.cli.consumers.textual.app.MoteApp`
splices into its ``CSS``.

Glyphs (``BULLET`` / ``BRANCH`` / ``CHECK`` / …) and the prompt symbol are simply
re-exported from the neutral render palette — one figure set for every host.
"""

from __future__ import annotations

from mote.cli.consumers.render.palette import (  # re-export the shared figure set
    BRANCH,
    BULLET,
    CHECK,
    COMPACT,
    CROSS,
    MEDIA,
    NOTE,
    PLAY,
    PROMPT_SYMBOL,
    RETRY,
    SCISSORS,
    SKIP,
    WARN,
    Palette,
)

# Textual accepts named colours ("grey50") only for a subset; map the palette's
# ``DIM`` (a rich colour name) to a concrete hex so Textual CSS always resolves.
_DIM_HEX = "#808080"


def textual_css_vars() -> dict[str, str]:
    """Return the Textual ``$variable → value`` map derived from :class:`Palette`.

    Keys are Textual variable names (without the leading ``$``); values are hex
    colours. The single numeric source is :class:`Palette`, so tuning a colour
    there re-tints both hosts.
    """
    dim = Palette.DIM if str(Palette.DIM).startswith("#") else _DIM_HEX
    return {
        "brand": Palette.BRAND,
        "success": Palette.SUCCESS,
        "error": Palette.ERROR,
        "warning": Palette.WARNING,
        "dim": dim,
        "diff-add": Palette.DIFF_ADD,
        "diff-del": Palette.DIFF_DEL,
        "question": Palette.QUESTION,
    }


def textual_css_var_block() -> str:
    """Render :func:`textual_css_vars` as Textual ``$name: value;`` lines.

    Textual has no top-level ``:root``-style block for user variables in the
    app ``CSS`` string; instead app-level variables are supplied via
    ``App.get_css_variables`` (see :class:`MoteApp`). This helper is kept for
    diagnostics / direct-splice fallbacks.
    """
    return "\n".join(f"${name}: {value};" for name, value in textual_css_vars().items())


__all__ = [
    "textual_css_vars",
    "textual_css_var_block",
    "Palette",
    "BULLET",
    "BRANCH",
    "CHECK",
    "CROSS",
    "PLAY",
    "SKIP",
    "MEDIA",
    "WARN",
    "RETRY",
    "NOTE",
    "COMPACT",
    "SCISSORS",
    "PROMPT_SYMBOL",
]
