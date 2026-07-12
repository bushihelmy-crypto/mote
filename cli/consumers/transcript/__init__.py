#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""``mote.cli.consumers.transcript`` — the shared event-orchestration state machine.

A neutral layer between the ``ViewEvent`` contract and the two rich human hosts
(the scrolling terminal and the full-screen Textual app): the
:class:`TranscriptReducer` folds the ``ViewEvent`` stream into a host-blind
:class:`TranscriptOp` stream, and each host implements a thin
:class:`RenderSurface` that lands those ops with its own primitives. The
:class:`SurfaceDriver` packages the pair as a ``BaseConsumer``.

Sits in ``consumers/`` (not ``contracts/``) because the reducer reads
``fold_mode`` from ``consumers/render/builders`` — a dependency ``contracts/``
(below it) may not take. It depends only on ``contracts/view`` +
``consumers/render/builders``; it never imports a specific host and never
reaches upward.
"""

from mote.cli.consumers.transcript.driver import SurfaceDriver, apply_ops
from mote.cli.consumers.transcript.ops import (
    AddToGroup,
    AppendDelta,
    ClearForCompaction,
    ClearRetry,
    ClearTranscript,
    CloseBlock,
    CompleteInGroup,
    FlushGroup,
    OpenBlock,
    OpenGroup,
    RenderApproval,
    RenderError,
    RenderFileDiff,
    RenderMedia,
    RenderNotice,
    RenderQuestion,
    RenderSessionList,
    RenderSystemReminder,
    RenderTaskProgress,
    RenderUserMessage,
    SetRetry,
    SetThinking,
    ToolCompleted,
    ToolStarted,
    TranscriptOp,
    Truncation,
    UpdateUsage,
)
from mote.cli.consumers.transcript.reducer import TranscriptReducer
from mote.cli.consumers.transcript.surface import BaseSurface, RenderSurface

__all__ = [
    "TranscriptReducer",
    "RenderSurface",
    "BaseSurface",
    "SurfaceDriver",
    "apply_ops",
    "TranscriptOp",
    "Truncation",
    "OpenBlock",
    "AppendDelta",
    "CloseBlock",
    "RenderUserMessage",
    "ToolStarted",
    "ToolCompleted",
    "OpenGroup",
    "AddToGroup",
    "CompleteInGroup",
    "FlushGroup",
    "RenderMedia",
    "RenderFileDiff",
    "RenderTaskProgress",
    "RenderNotice",
    "RenderSystemReminder",
    "RenderError",
    "RenderQuestion",
    "RenderApproval",
    "RenderSessionList",
    "SetThinking",
    "SetRetry",
    "ClearRetry",
    "UpdateUsage",
    "ClearForCompaction",
    "ClearTranscript",
]
