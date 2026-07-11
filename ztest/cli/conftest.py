#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Shared fixtures/helpers for the mote.cli test suite.

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
from mote.cli.contracts.base import BaseConsumer
from mote.cli.contracts.view import Capabilities
from mote.common.events.types import (
    COMPACTION_CHECKPOINT,
    LLM_ERROR,
    LLM_RETRY,
    LLM_STREAM_DELTA,
    LLM_STREAM_END,
    MESSAGE_APPENDED,
    POST_TOOL_USE,
    PRE_TOOL_USE,
    TASK_PROGRESS,
)


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


def ev_pre_tool(tool_name: str, tool_input: Optional[dict] = None, tool_use_id: str = "tu-1") -> AgentEvt:
    return AgentEvt(PRE_TOOL_USE, tool_name=tool_name, tool_input=tool_input or {}, tool_use_id=tool_use_id)


def ev_post_tool(
    tool_name: str,
    tool_response: Any,
    tool_use_id: str = "tu-1",
    tool_input: Optional[dict] = None,
    success: Optional[bool] = None,
    media: Optional[list] = None,
    file_changes: Optional[list] = None,
) -> AgentEvt:
    """Build a POST_TOOL_USE event.

    ``success=None`` (default) omits the structured field entirely, mirroring a
    legacy event so the projector's prefix-sniff fallback is exercised. Passing
    an explicit bool stamps ``event.success`` — the P0 structured fact the
    projector reads in preference to sniffing.

    ``media=None`` (default) likewise omits the ``media`` field, so the projector's
    legacy image-sniff fallback is exercised. Passing a list (possibly empty)
    stamps ``event.media`` — the structured media facts the projector folds into
    ``MediaBlock``\\s in preference to sniffing.

    ``file_changes=None`` (default) omits the field; passing a list of
    ``FileChange`` stamps ``event.file_changes`` — the structured file-change
    facts the projector folds into ``FileDiffBlock``\\s.
    """
    fields = dict(
        tool_name=tool_name,
        tool_response=tool_response,
        tool_use_id=tool_use_id,
        tool_input=tool_input or {},
    )
    if success is not None:
        fields["success"] = success
    if media is not None:
        fields["media"] = media
    if file_changes is not None:
        fields["file_changes"] = file_changes
    return AgentEvt(POST_TOOL_USE, **fields)


def ev_progress(stage: str = "", status: str = "", detail: str = "") -> AgentEvt:
    return AgentEvt(TASK_PROGRESS, stage=stage, status=status, detail=detail)


def ev_compaction(summary: str = "", messages: Optional[list] = None) -> AgentEvt:
    """A COMPACTION_CHECKPOINT: the engine rebuilt history + its recap ``summary``.

    Mirrors mote's ``CompactionCheckpointEvent`` (``messages`` = the rebuilt
    history, ``summary`` = the model-generated recap) so the projector's
    compaction-boundary fold is exercised.
    """
    return AgentEvt(COMPACTION_CHECKPOINT, messages=messages or [], summary=summary)


def ev_error(error: str = "", error_type: str = "") -> AgentEvt:
    return AgentEvt(LLM_ERROR, error=error, error_type=error_type)


def ev_retry(
    attempt: int = 1,
    max_attempts: int = 6,
    delay_ms: float = 2000.0,
    error: str = "",
    error_type: str = "",
) -> AgentEvt:
    return AgentEvt(
        LLM_RETRY,
        attempt=attempt,
        max_attempts=max_attempts,
        delay_ms=delay_ms,
        error=error,
        error_type=error_type,
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
