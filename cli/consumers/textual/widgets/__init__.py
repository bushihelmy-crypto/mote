#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Textual widgets for the full-screen TUI transcript + chrome.

This package splits the widgets by concern into sibling modules and re-exports
every symbol here as a facade, so ``from mote.cli.consumers.textual.widgets
import X`` keeps resolving for all existing call sites:

- :mod:`.base` — the shared :class:`SelectableStatic` base (selection + Ctrl+click).
- :mod:`.transcript` — every transcript-area row/block widget + ``build_tool_parts``.
- :mod:`.status_bar` — the persistent :class:`StatusBar`.
- :mod:`.prompt_input` — the bottom :class:`PromptInput` + ``PROMPT_SYMBOL``.

The rich renderables the widgets display are produced by the SHARED
``mote.cli.consumers.render.builders`` so the terminal and Textual hosts never
diverge on look (§9.7 "format once").
"""

from __future__ import annotations

from mote.cli.consumers.textual.widgets.base import SelectableStatic
from mote.cli.consumers.textual.widgets.prompt_input import PROMPT_SYMBOL, PromptInput
from mote.cli.consumers.textual.widgets.status_bar import StatusBar, _format_tok  # noqa: F401  # re-export for tests
from mote.cli.consumers.textual.widgets.transcript import (
    ApprovalMarkerRow,
    AssistantBlock,
    CompactionSummaryRow,
    ConversationCompactedRow,
    ErrorRow,
    FileDiffRow,
    MediaRow,
    NoticeRow,
    QuestionMarkerRow,
    SessionListWidget,
    SystemReminderRow,
    TaskProgressRow,
    ToolCallWidget,
    ToolGroupWidget,
    UserMessageRow,
    build_tool_parts,
)

__all__ = [
    "SelectableStatic",
    "AssistantBlock",
    "build_tool_parts",
    "ToolCallWidget",
    "ToolGroupWidget",
    "UserMessageRow",
    "MediaRow",
    "FileDiffRow",
    "NoticeRow",
    "SystemReminderRow",
    "ConversationCompactedRow",
    "CompactionSummaryRow",
    "ErrorRow",
    "TaskProgressRow",
    "QuestionMarkerRow",
    "ApprovalMarkerRow",
    "SessionListWidget",
    "StatusBar",
    "PromptInput",
    "PROMPT_SYMBOL",
]
