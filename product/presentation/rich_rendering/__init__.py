#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""``mote.product.presentation.rich_rendering`` — the neutral, host-agnostic rich substrate.

Both rich-based hosts read their look from here so neither depends on the other:

* :mod:`~mote.product.presentation.rich_rendering.palette` — the single colour/glyph source
  (``Palette``, ``BULLET``/``BRANCH``/…, ``PROMPT_SYMBOL``). Rich-free, ANSI-free.
* :mod:`~mote.product.presentation.rich_rendering.builders` — console-free ``rich`` builders
  (``render_diff``/``build_table``/``tool_started_text``/…) that turn a
  ``ViewEvent`` into a renderable with no ``Console`` and no side effects.

The scrolling :class:`~mote.product.interfaces.terminal.consumer.TerminalConsumer`
and the full-screen :class:`~mote.product.interfaces.textual.app.MoteApp` (and, in
future, that same Textual app served over the web) all depend *downward* on this
package; host-specific presentation (the terminal's ANSI prompt/banner, the
textual widgets + CSS) stays in each host.
"""

from mote.product.presentation.rich_rendering.builders import (
    CONTENT_INDENT,
    RESULT_INDENT,
    USAGE_SEP,
    build_table,
    bullet_row,
    compaction_summary_text,
    conversation_compacted_text,
    format_usage_line,
    indent,
    linkify,
    render_diff,
    render_file_change,
    render_image,
    session_table,
    shimmer_text,
    sparkline,
    tool_body_syntax,
    tool_completed_text,
    tool_group_summary_text,
    tool_started_text,
    user_message_row,
)
from mote.product.presentation.rich_rendering.markdown import themed_markdown
from mote.product.presentation.rich_rendering.palette import (
    BRANCH,
    BULLET,
    CHECK,
    CROSS,
    MEDIA,
    PLAY,
    PROMPT_SYMBOL,
    SKIP,
    WARN,
    Palette,
)

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
    "PROMPT_SYMBOL",
    "CONTENT_INDENT",
    "RESULT_INDENT",
    "USAGE_SEP",
    "bullet_row",
    "indent",
    "linkify",
    "render_diff",
    "render_file_change",
    "render_image",
    "build_table",
    "tool_body_syntax",
    "tool_started_text",
    "tool_completed_text",
    "tool_group_summary_text",
    "shimmer_text",
    "sparkline",
    "user_message_row",
    "conversation_compacted_text",
    "compaction_summary_text",
    "session_table",
    "format_usage_line",
    "themed_markdown",
]
