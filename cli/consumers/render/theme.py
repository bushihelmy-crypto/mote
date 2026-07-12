#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Markdown colour theme — maps :class:`Palette` tokens onto rich's ``markdown.*``.

rich's :class:`~rich.markdown.Markdown` resolves every element's colour at render
time by looking up a style name (``markdown.h1``, ``markdown.code``, …) on the
active console. Out of the box those names carry rich's own defaults (magenta
blockquotes, cyan-on-black code, bright-yellow bullets, bright-blue links) which
clash with our brand-orange look. This module re-binds every one of those names
to a :class:`Palette` token, so a Markdown block reads the same as the rest of the
transcript — the accent orange on h1/list-markers, a stepped-down heading hierarchy
below it, and dim secondary chrome (rules, quotes).

Following MarkText/Muya's "Markdown element colours" convention, colour choice is
*semantic per element*, not ad-hoc:

* headings step **down** from the brand accent (``h1`` = brand) through cooler,
  progressively dimmer tones so the outline hierarchy is legible at a glance;
* the accent is reused on the structural markers a reader scans for — list bullets,
  numbers, and links — to tie the block back to the app's identity;
* body chrome that should recede — block quotes, horizontal rules — is ``DIM``;
* inline/blocked ``code`` drops rich's black background (which fought our own
  surface colour) for a plain brand-tinted foreground.

The theme is intentionally a plain data object: hosts apply it locally (see
:mod:`mote.cli.consumers.render.markdown`) rather than mutating any global
console, so a terminal stream and the Textual app render byte-identically.
"""

from __future__ import annotations

from rich.theme import Theme

from mote.cli.consumers.render.palette import Palette

# Heading hierarchy: h1 is the brand accent; h2→h6 step toward the dim secondary
# tone so nested outlines stay readable without shouting. h1's border colour is
# themed too, but the box itself is dropped structurally by ``BrandHeading``.
_MARKDOWN_STYLES = {
    "markdown.h1": f"bold {Palette.BRAND}",
    "markdown.h1.border": Palette.BRAND,
    "markdown.h2": f"bold {Palette.DIFF_HUNK}",
    "markdown.h3": f"bold {Palette.QUESTION}",
    "markdown.h4": f"bold {Palette.SUCCESS}",
    "markdown.h5": f"bold {Palette.WARNING}",
    "markdown.h6": f"bold {Palette.DIM}",
    # Structural markers the eye scans → reuse the accent.
    "markdown.item.bullet": f"bold {Palette.BRAND}",
    "markdown.item.number": f"bold {Palette.BRAND}",
    "markdown.link": f"underline {Palette.DIFF_HUNK}",
    "markdown.link_url": f"underline {Palette.DIM}",
    # Recede chrome.
    "markdown.block_quote": Palette.DIM,
    "markdown.hr": Palette.DIM,
    # Code: drop rich's black background for a plain brand-tinted foreground.
    "markdown.code": Palette.BRAND,
    "markdown.code_block": Palette.BRAND,
    # Emphasis kept structural (bold/italic already encode it); no colour change.
}

MARKDOWN_THEME = Theme(_MARKDOWN_STYLES, inherit=True)


__all__ = ["MARKDOWN_THEME"]
