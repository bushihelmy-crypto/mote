#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Console-free rich *builders* shared by every rich host.

The :class:`~mote.product.interfaces.terminal.consumer.TerminalConsumer` used to
own both the incremental ``Live`` streaming machinery *and* a set of pure
``rich`` renderable builders (colour a diff, build a TSV table, format the usage
line, compose a tool's headline/summary text). The builders are **console-free**
— they take a ``ViewEvent`` (or raw string) and return a ``rich`` renderable, with
no ``Console`` and no side effects — so any host (the scrolling terminal *and*
the full-screen Textual TUI, which mounts these renderables into its widgets)
reuses the exact same look.

This package is that shared renderer, living in the neutral ``consumers/render``
package so neither host depends on the other. It splits the builders by concern
into sibling modules and re-exports every public symbol here as a facade, so
``from mote.product.presentation.rich_rendering.builders import X`` keeps resolving for all
existing call sites:

- :mod:`._rich` — the single optional-``rich`` import point (``_HAS_RICH`` + rich
  symbols reused by the other submodules).
- :mod:`.diff` — the unified-diff colouriser (``render_diff``/``render_file_change``).
- :mod:`.image` — protocol-free half-block inline images (``render_image``).
- :mod:`.effects` — shimmer 微光 + sparkline 图表 mini-renderables.
- :mod:`.core` — everything else (layout, tool lines, group summary, usage line…).

``rich`` is optional — every builder is guarded by ``_HAS_RICH`` and the
consumers degrade gracefully when it is absent (§9.10).
"""

from __future__ import annotations

from mote.product.presentation.rich_rendering.builders._rich import _HAS_RICH  # noqa: F401  # facade re-export
from mote.product.presentation.rich_rendering.builders.activity import (
    activity_header,
    activity_outcome,
    activity_topology,
)
from mote.product.presentation.rich_rendering.builders.common import (
    CONTENT_INDENT,
    RESULT_INDENT,
    bullet_row,
    indent,
    notice_style,
)
from mote.product.presentation.rich_rendering.builders.diff import render_diff, render_file_change
from mote.product.presentation.rich_rendering.builders.effects import interpolate_color, shimmer_text, sparkline
from mote.product.presentation.rich_rendering.builders.image import IMAGE_MAX_COLS, IMAGE_MAX_ROWS, render_image
from mote.product.presentation.rich_rendering.builders.message import (
    compaction_summary_text,
    conversation_compacted_text,
    linkify,
    system_reminder_text,
    user_message_row,
)
from mote.product.presentation.rich_rendering.builders.session import session_table
from mote.product.presentation.rich_rendering.builders.tool import (
    FoldMode,
    build_table,
    file_change_caption,
    file_change_verb,
    fold_mode,
    fold_note,
    fold_note_str,
    is_rejection,
    media_caption,
    render_result_detail,
    task_progress_text,
    tool_body_syntax,
    tool_completed_text,
    tool_group_summary_text,
    tool_started_text,
)
from mote.product.presentation.rich_rendering.builders.usage import USAGE_SEP, format_usage_line

__all__ = [
    "CONTENT_INDENT",
    "RESULT_INDENT",
    "USAGE_SEP",
    "bullet_row",
    "indent",
    "linkify",
    "system_reminder_text",
    "render_diff",
    "render_file_change",
    "build_table",
    "tool_body_syntax",
    "user_message_row",
    "interpolate_color",
    "shimmer_text",
    "sparkline",
    "tool_started_text",
    "tool_completed_text",
    "is_rejection",
    "FoldMode",
    "fold_mode",
    "tool_group_summary_text",
    "notice_style",
    "render_result_detail",
    "fold_note",
    "fold_note_str",
    "file_change_verb",
    "file_change_caption",
    "media_caption",
    "task_progress_text",
    "activity_header",
    "activity_topology",
    "activity_outcome",
    "conversation_compacted_text",
    "compaction_summary_text",
    "session_table",
    "format_usage_line",
    "render_image",
    "IMAGE_MAX_COLS",
    "IMAGE_MAX_ROWS",
]
