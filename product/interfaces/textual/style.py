#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Textual CSS variables derived from the single :class:`Palette`.

The Textual TUI must read the **same** colour tokens as the rich terminal so the
two hosts never diverge on brand orange / success green / diff colours. Rather
than a second colour source, :func:`textual_css_vars` projects the one neutral
:class:`~mote.product.presentation.rich_rendering.palette.Palette` (hex tokens) into Textual's
``$variable`` map, which :class:`~mote.product.interfaces.textual.app.MoteApp`
splices into its ``CSS``.

Glyphs (``BULLET`` / ``BRANCH`` / ``CHECK`` / …) and the prompt symbol are simply
re-exported from the neutral render palette — one figure set for every host.
"""

from __future__ import annotations

from mote.product.presentation.rich_rendering.palette import (  # re-export the shared figure set
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

try:
    from textual.theme import Theme
except ImportError:  # pragma: no cover - optional cli extra
    Theme = None

# Textual accepts named colours ("grey50") only for a subset; map the palette's
# ``DIM`` (a rich colour name) to a concrete hex so Textual CSS always resolves.
_DIM_HEX = "#808080"

# --- Warm dark surface tokens (the TUI's "chrome" background) -----------------
# Textual's built-in ``textual-dark`` theme leaves ``background``/``surface``
# unset, so it auto-derives a near-black (#0e0e0e-ish) canvas that reads as a
# flat black slab. These warm neutrals — a hair of the brand orange bled into
# near-black — give the transcript a softer, "toasted" charcoal that sits with
# the accent instead of fighting it (stone-900 family, tuned toward the brand).
THEME_NAME = "mote-monokai"
# The classic Monokai canvas (cmder's default) — a neutral olive-charcoal, not
# a warm brown. Each step is a distinct raised surface off the same base hue so
# inputs / panels read as lifted planes without any muddy tint.
_BG = "#000000"  # app canvas — black
_SURFACE = "#120231"  # cards / inputs — clearly lifted off the canvas
_PANEL = "#787969"  # raised chrome
_FOREGROUND = "#f8f8f2"  # Monokai off-white body text

# The bottom status bar band — an orange-yellow anchoring slab under the warm
# transcript, with black text for contrast. Exposed as ``$status-bg`` /
# ``$status-fg`` so the ``StatusBar`` widget CSS reads them from the shared token
# map.
STATUS_BG = "#e8a317"  # status bar band — orange-yellow
STATUS_FG = "#000000"  # status bar text — black for contrast on orange-yellow


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
        "status-bg": STATUS_BG,
        "status-fg": STATUS_FG,
    }


def mote_theme():
    """Build the Monokai-dark :class:`textual.theme.Theme` the app registers.

    Keyed off the same :class:`Palette` the CSS vars read, so brand orange is the
    ``primary``/``accent`` while the canvas uses the Monokai olive-charcoal surface
    tokens above instead of Textual's auto-derived near-black. Imported lazily so
    this module stays cheap for the non-Textual hosts that only want the CSS vars.
    """
    if Theme is None:
        raise RuntimeError("The Textual theme requires the 'textual' optional dependency.")

    return Theme(
        name=THEME_NAME,
        primary=Palette.BRAND,
        secondary=Palette.SHIMMER,
        accent=Palette.BRAND,
        warning=Palette.WARNING,
        error=Palette.ERROR,
        success=Palette.SUCCESS,
        foreground=_FOREGROUND,
        background=_BG,
        surface=_SURFACE,
        panel=_PANEL,
        dark=True,
    )


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
    "mote_theme",
    "THEME_NAME",
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
