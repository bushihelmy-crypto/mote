#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""``cli/view`` — the human protocol (窄腰之一).

The single fold point ``AgentEvent → ViewEvent`` is the host-specific
:class:`~metagpt.cli.view.projector.ViewProjector`. The human *contract* it folds
into (``ViewEvent`` + the capability downgrade rules) is shared across hosts and
lives in :mod:`metagpt.cli.common.view`; the reusable fan-out plumbing
(:class:`~metagpt.cli.common.base.BaseProjector`) lives in
:mod:`metagpt.cli.common.base`. This package re-exports them so existing callers
keep importing ``from metagpt.cli.view import ...``.
"""

from metagpt.cli.common.base import BaseProjector
from metagpt.cli.common.view import (
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
from metagpt.cli.view.projector import ViewProjector

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
