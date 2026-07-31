#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""``cli/view`` — the human protocol (窄腰之一).

The single fold point ``AgentEvent → ViewEvent`` is the host-specific
:class:`~mote.product.presentation.projection.projector.ViewProjector`. The human *contract* it folds
into (``ViewEvent`` + the capability downgrade rules) is shared across hosts and
lives in :mod:`mote.product.presentation.events`; the reusable fan-out plumbing
(:class:`~mote.product.cli.contracts.base.BaseProjector`) lives in
:mod:`mote.product.cli.contracts.base`. This package re-exports them so existing callers
keep importing ``from mote.product.presentation.projection import ...``.
"""

from mote.product.presentation.events import (
    STRUCTURED_CAPS,
    TERMINAL_CAPS,
    TEXTUAL_CAPS,
    AttemptStreamCommitted,
    AttemptStreamDiscarded,
    AttemptStreamInterrupted,
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
    RuntimeDurabilityStatus,
    SystemReminder,
    TaskProgress,
    ToolCallCompleted,
    ToolCallStarted,
    ViewEvent,
)
from mote.product.presentation.projection.base import BaseProjector
from mote.product.presentation.projection.projector import ViewProjector

__all__ = [
    "ViewEvent",
    "MessageBlockStarted",
    "MessageBlockDelta",
    "MessageBlockCompleted",
    "AttemptStreamCommitted",
    "AttemptStreamDiscarded",
    "AttemptStreamInterrupted",
    "ReasoningDelta",
    "ToolCallStarted",
    "ToolCallCompleted",
    "TaskProgress",
    "Notice",
    "ErrorRaised",
    "QuestionAsked",
    "RetryStatus",
    "RuntimeDurabilityStatus",
    "SystemReminder",
    "ConversationCompacted",
    "Capabilities",
    "CapabilityAdapter",
    "TERMINAL_CAPS",
    "TEXTUAL_CAPS",
    "STRUCTURED_CAPS",
    "BaseProjector",
    "ViewProjector",
]
