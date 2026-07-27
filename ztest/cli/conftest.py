#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Shared fixtures/helpers for the mote.product.cli test suite.

The projector folds the *core* ``AgentEvent`` spine; tests don't need the real
event classes — they only need objects that expose the attributes the projector
reads (``name`` + a handful of payload fields). ``AgentEvt`` is that duck-typed
stand-in, and the ``ev_*`` builders mint the specific event shapes the projector
branches on. A tiny ``RecordingConsumer`` captures what reaches a consumer so the
``BaseProjector`` plumbing can be asserted end-to-end.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, List, Optional

import pytest

from mote.contracts.events.types import (
    BUDGET,
    CONTEXT_COMPACTED,
    LLM_STREAM_DELTA,
    LLM_STREAM_END,
    MESSAGE_APPENDED,
    MODEL_ATTEMPT_FINISHED,
    TASK_PROGRESS,
    TOOL_CALL_FINISHED,
    TOOL_INVOCATION_STARTED,
)
from mote.product.cli.contracts.base import BaseConsumer
from mote.product.cli.contracts.view import Capabilities


class AgentEvt:
    """Duck-typed AgentEvent: just a ``name`` plus arbitrary payload attributes."""

    def __init__(self, name: str, **fields: Any) -> None:
        self.name = name
        for key, value in fields.items():
            setattr(self, key, value)


def ev_delta(token: str) -> AgentEvt:
    return AgentEvt(LLM_STREAM_DELTA, token=token)


def ev_stream_end() -> AgentEvt:
    return AgentEvt(LLM_STREAM_END)


def ev_message(role: str, content: str) -> AgentEvt:
    return AgentEvt(MESSAGE_APPENDED, message=SimpleNamespace(role=role, content=content))


def ev_system_reminder(inner: str) -> AgentEvt:
    """A user MESSAGE_APPENDED whose content is a ``<system-reminder>`` envelope.

    Mirrors mote's turn-context bus output (the framework writes the merged
    per-turn block into history as a user message wrapped in these tags).
    """
    content = f"<system-reminder>\n{inner}\n</system-reminder>"
    return AgentEvt(MESSAGE_APPENDED, message=SimpleNamespace(role="user", content=content))


def ev_tool_started(
    tool_name: str,
    tool_input: Optional[dict] = None,
    tool_use_id: str = "tu-1",
) -> AgentEvt:
    return AgentEvt(
        TOOL_INVOCATION_STARTED,
        tool_name=tool_name,
        tool_input=tool_input or {},
        tool_use_id=tool_use_id,
    )


def ev_post_tool(
    tool_name: str,
    tool_response: Any,
    tool_use_id: str = "tu-1",
    tool_input: Optional[dict] = None,
    success: bool = True,
    media: Optional[list] = None,
    file_changes: Optional[list] = None,
    error: Any = None,
) -> AgentEvt:
    """Build a TOOL_CALL_FINISHED fixture for projector tests.

    The core stamps ``outcome`` / ``media`` / ``file_changes`` on the event;
    ``success`` is only this helper's readable input and defaults to True.
    ``media`` / ``file_changes`` to empty lists. The projector reads these
    structured facts directly (no text sniffing).

    ``success=False`` marks a failed call; ``media`` / ``file_changes`` accept a
    list of ``ToolMedia`` / ``FileChange`` the projector folds into ``MediaBlock``
    / ``FileDiffBlock``\\s. ``error`` carries an optional structured ``ErrorReport``
    whose code/type/retryable/recovery the projector reads onto the completion.
    """
    return AgentEvt(
        TOOL_CALL_FINISHED,
        tool_name=tool_name,
        tool_response=tool_response,
        tool_use_id=tool_use_id,
        tool_input=tool_input or {},
        outcome="succeeded" if success else "failed",
        media=media or [],
        file_changes=file_changes or [],
        error=error,
    )


def ev_progress(stage: str = "", status: str = "", detail: str = "") -> AgentEvt:
    return AgentEvt(TASK_PROGRESS, stage=stage, status=status, detail=detail)


def ev_budget(spend: float = 0.0, limit: float = 0.0, fraction: float = 0.0, stopped: bool = False) -> AgentEvt:
    """A BUDGET event — soft warning (``stopped=False``) or hard stop (True)."""
    return AgentEvt(BUDGET, spend=spend, limit=limit, fraction=fraction, stopped=stopped)


def ev_compaction(
    summary: str = "",
    model_context_messages: Optional[list] = None,
) -> AgentEvt:
    """A CONTEXT_COMPACTED event carrying the active model projection."""

    return AgentEvt(
        CONTEXT_COMPACTED,
        model_context_messages=model_context_messages or [],
        summary=summary,
    )


def ev_error(error: str = "", error_type: str = "") -> AgentEvt:
    return AgentEvt(
        MODEL_ATTEMPT_FINISHED,
        state="failed",
        failure_reason=error or error_type,
    )


class RecordingConsumer(BaseConsumer):
    """Captures every ViewEvent reaching it (post capability-adapter)."""

    def __init__(self, caps: Optional[Capabilities] = None) -> None:
        self.capabilities = caps or Capabilities()
        self.events: List[Any] = []

    async def handle(self, ev: Any) -> None:  # async dispatch path
        self.events.append(ev)

    def handle_sync(self, ev: Any) -> None:  # sync (emit_sync) path
        self.events.append(ev)


@pytest.fixture
def recording_consumer():
    return RecordingConsumer()
