#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""``cli/view`` — the human protocol (窄腰之一).

The single fold point ``AgentEvent → ViewEvent`` is the host-specific
:class:`~mote.cli.view.projector.ViewProjector`. The human *contract* it folds
into (``ViewEvent`` + the capability downgrade rules) is shared across hosts and
lives in :mod:`mote.cli.contracts.view`; the reusable fan-out plumbing
(:class:`~mote.cli.contracts.base.BaseProjector`) lives in
:mod:`mote.cli.contracts.base`. This package re-exports them so existing callers
keep importing ``from mote.cli.view import ...``.
"""

from mote.cli.contracts.base import BaseProjector
from mote.cli.contracts.view import (
    STRUCTURED_CAPS,
    TERMINAL_CAPS,
    Capabilities,
    CapabilityAdapter,
    ConversationCompacted,
    ErrorRaised,
    MessageBlockCompleted,
    MessageBlockDelta,
    MessageBlockStarted,
    Notice,
    QuestionAsked,
    ReasoningDelta,
    RetryStatus,
    SystemReminder,
    TaskProgress,
    ToolCallCompleted,
    ToolCallStarted,
    ViewEvent,
)
from mote.cli.view.projector import ViewProjector

__all__ = [
    "ViewEvent",
    "MessageBlockStarted",
    "MessageBlockDelta",
    "MessageBlockCompleted",
    "ReasoningDelta",
    "ToolCallStarted",
    "ToolCallCompleted",
    "TaskProgress",
    "Notice",
    "ErrorRaised",
    "QuestionAsked",
    "RetryStatus",
    "SystemReminder",
    "ConversationCompacted",
    "Capabilities",
    "CapabilityAdapter",
    "TERMINAL_CAPS",
    "STRUCTURED_CAPS",
    "BaseProjector",
    "ViewProjector",
]
