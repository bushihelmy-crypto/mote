#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Colour tokens + glyphs — the single source of truth for every rich host's look.

Aligned with claude-code's aesthetic (brand-orange accent, light bullet +
tree-branch layout instead of heavy boxes). Every rich-based host — the scrolling
:class:`~mote.cli.consumers.terminal.consumer.TerminalConsumer` and the
full-screen :class:`~mote.cli.consumers.textual.app.MoteApp` (and, in future,
the same Textual app served over the web) — reads its colours and figures from
here, so the "look" is tuned in ONE neutral place that depends on no host.

Colours are hex strings (``rich`` accepts ``#rrggbb``); each maps to a
claude-code theme token so the hosts read the same way:

* ``BRAND``   — the accent orange (claude-code ``claude`` ``rgb(215,119,87)``),
  used for the assistant/tool bullet and tool names.
* ``SUCCESS`` / ``ERROR`` / ``WARNING`` — status colours (``rgb(44,122,57)`` /
  ``rgb(171,43,63)`` / ``rgb(150,108,30)``).
* ``DIFF_ADD`` / ``DIFF_DEL`` — diff line colours (``rgb(105,219,124)`` /
  ``rgb(255,168,180)``).
* ``DIM`` — secondary / folded-affordance text.

Glyphs mirror claude-code's figure set: ``●`` marks an assistant turn / a tool
invocation, ``⎿`` prefixes a tool's result line (the implied tree branch).

This module is intentionally rich-free and ANSI-free — pure string/data tokens —
so it is safe to import from any host (rich, textual, or a plain-stream port).
"""

from __future__ import annotations


class Palette:
    """claude-code-aligned colour tokens (hex, rich-compatible)."""

    BRAND = "#d77757"  # accent orange — assistant/tool bullet, tool names
    SHIMMER = "#f59575"  # lighter brand — the moving shimmer band + running pulse
    SUCCESS = "#2c7a39"  # green — ok result / success notice
    ERROR = "#ab2b3f"  # red — failed result / error
    WARNING = "#966c1e"  # amber — approval / warning notice
    DIM = "grey50"  # secondary text, folded affordance, usage line
    DIFF_ADD = "#69db7c"  # diff added line — bright fg on the add bar
    DIFF_DEL = "#ffa8b4"  # diff removed line — bright fg on the del bar
    # Filled diff bars (claude-code look): a dark tinted background spans the
    # whole changed line; a brighter "emph" background highlights the exact
    # word-level spans that actually changed within a -/+ pair.
    DIFF_ADD_BG = "#12291b"  # add line background (dark green)
    DIFF_DEL_BG = "#2d151b"  # del line background (dark red)
    DIFF_ADD_EMPH_BG = "#1f6b3a"  # changed-word background on an add line
    DIFF_DEL_EMPH_BG = "#6e2233"  # changed-word background on a del line
    DIFF_HUNK = "#4a9eda"  # @@ hunk header (cyan)
    QUESTION = "#b48ead"  # posed-question marker (soft magenta)
    LINK = "#4a9eda"  # clickable URL (cyan, underlined by the linkifier)


# Glyphs (claude-code figure set).
BULLET = "\u25cf"  # ● — assistant turn / tool invocation marker
BRANCH = "\u23bf"  # ⎿ — tool result branch (implied tree)
CHECK = "\u2713"  # ✓ — success
CROSS = "\u2717"  # ✗ — failure
PLAY = "\u25b6"  # ▶ — running
SKIP = "\u2298"  # ⊘ — skipped / other
MEDIA = "\u29c9"  # ⧉ — media reference
WARN = "\u26a0"  # ⚠ — approval gate
RETRY = "\u27f3"  # ⟳ — transient LLM-retry countdown
NOTE = "\u2691"  # ⚑ — framework-injected system-reminder context
COMPACT = "\u273b"  # ✻ — conversation history was compacted (claude-code marker)
SCISSORS = "\u2702"  # ✂ — hard truncation (output too large, persisted to disk)

# Prompt glyph (claude-code uses the heavier ``❯`` chevron, not ``›``). The
# host-specific decoration (ANSI colour for the terminal port, ``$brand`` border
# for the textual input) is applied by each host; the bare symbol lives here so
# both read the same figure.
PROMPT_SYMBOL = "\u276f"  # ❯


__all__ = [
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
