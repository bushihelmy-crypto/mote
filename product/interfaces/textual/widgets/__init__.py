#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Textual widgets for the full-screen TUI transcript + chrome.

This package splits the widgets by concern into sibling modules and re-exports
every symbol here as a facade, so ``from mote.product.interfaces.textual.widgets
import X`` keeps resolving for all existing call sites:

- :mod:`.base` — the shared :class:`SelectableStatic` base (selection + Ctrl+click).
- :mod:`.transcript` — every transcript-area row/block widget + ``build_tool_parts``.
- :mod:`.status_bar` — the persistent :class:`StatusBar`.
- :mod:`.prompt_input` — the bottom :class:`PromptInput` + ``PROMPT_SYMBOL``.

The rich renderables the widgets display are produced by the SHARED
``mote.product.presentation.rich_rendering.builders`` so the terminal and Textual hosts never
diverge on look (§9.7 "format once").
"""

from __future__ import annotations

from mote.product.interfaces.textual.widgets.activity import ActivityWidget
from mote.product.interfaces.textual.widgets.base import SelectableStatic
from mote.product.interfaces.textual.widgets.prompt_input import PROMPT_SYMBOL, PromptInput
from mote.product.interfaces.textual.widgets.status_bar import (  # noqa: F401  # re-export for tests
    StatusBar,
    _format_tok,
)
from mote.product.interfaces.textual.widgets.transcript import (
    ApprovalMarkerRow,
    AssistantBlock,
    CompactionSummaryRow,
    ConversationCompactedRow,
    ErrorRow,
    FileDiffRow,
    FoldableRow,
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
    "ActivityWidget",
    "AssistantBlock",
    "build_tool_parts",
    "FoldableRow",
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
