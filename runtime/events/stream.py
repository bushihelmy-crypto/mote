#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
LLM stream emission.

Streamed LLM tokens are emitted onto the active telemetry runtime as
:class:`~mote.contracts.events.types.LLMStreamDeltaEvent` observation events.
Whoever wants to mirror them live (the REPL renderer) or forward them (the web
reporter) subscribes to Telemetry — there is no process-global sink anymore, so
screen and disk can no longer diverge.

``log_llm_stream`` stays a plain sync function (the LLM providers call it from
inside their ``async for`` chunk loops) and uses Telemetry's sync fire-and-forget
delivery. It no-ops when no telemetry runtime is bound, so the router never has
to know who, if anyone, is listening.

This lives in the ``events`` package (not ``logs``) because it is purely an
telemetry concern: it depends only on ``events`` primitives. Keeping it here lets
``logs`` stay a pure leaf, so the dependency edge runs one-way ``events → logs``
(Telemetry uses ``logger``) with no cycle back.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Iterator

from mote.contracts.events.types import (
    LLMStreamCommittedEvent,
    LLMStreamDeltaEvent,
    LLMStreamDiscardedEvent,
    LLMStreamInterruptedEvent,
)
from mote.kernel.output_stream import feed_output_stream
from mote.runtime.events.context import observe_event_sync


@dataclass
class AttemptStreamBuffer:
    """Call-local provisional deltas captured from one provider attempt."""

    model_call_id: str = ""
    attempt_id: str = ""
    chunks: list[str] = field(default_factory=list)

    def append(self, chunk: str) -> None:
        if chunk:
            self.chunks.append(chunk)
            observe_event_sync(
                LLMStreamDeltaEvent(
                    token=chunk,
                    model_call_id=self.model_call_id,
                    attempt_id=self.attempt_id,
                    sequence=len(self.chunks),
                    provisional=True,
                )
            )


_active_buffer: ContextVar[AttemptStreamBuffer | None] = ContextVar(
    "mote_attempt_stream_buffer",
    default=None,
)


@contextmanager
def capture_attempt_stream(
    enabled: bool,
    *,
    model_call_id: str = "",
    attempt_id: str = "",
) -> Iterator[AttemptStreamBuffer | None]:
    """Buffer provider deltas until Runtime accepts or rejects the attempt."""

    buffer = AttemptStreamBuffer(model_call_id=model_call_id, attempt_id=attempt_id) if enabled else None
    token = _active_buffer.set(buffer)
    try:
        yield buffer
    finally:
        _active_buffer.reset(token)


def commit_attempt_stream(
    buffer: AttemptStreamBuffer | None,
    *,
    model_call_id: str,
    attempt_id: str,
) -> None:
    """Publish an accepted attempt in order; uncommitted buffers are discarded."""

    if buffer is None:
        return
    for chunk in buffer.chunks:
        feed_output_stream(chunk)
    observe_event_sync(
        LLMStreamCommittedEvent(
            model_call_id=model_call_id,
            attempt_id=attempt_id,
            chunk_count=len(buffer.chunks),
        )
    )


def discard_attempt_stream(
    buffer: AttemptStreamBuffer | None,
    *,
    model_call_id: str,
    attempt_id: str,
    reason: str,
) -> None:
    """Publish the terminal rejection of one buffered attempt stream."""

    if buffer is None:
        return
    observe_event_sync(
        LLMStreamDiscardedEvent(
            model_call_id=model_call_id,
            attempt_id=attempt_id,
            chunk_count=len(buffer.chunks),
            reason=reason,
        )
    )


def interrupt_attempt_stream(
    buffer: AttemptStreamBuffer | None,
    *,
    model_call_id: str,
    attempt_id: str,
    reason: str = "cancelled",
) -> None:
    """Publish that cancellation ended a stream without an accepted response."""

    if buffer is None:
        return
    observe_event_sync(
        LLMStreamInterruptedEvent(
            model_call_id=model_call_id,
            attempt_id=attempt_id,
            chunk_count=len(buffer.chunks),
            reason=reason,
        )
    )


def _emit_llm_stream(
    msg: str,
    *,
    model_call_id: str = "",
    attempt_id: str = "",
    sequence: int = 0,
) -> None:
    observe_event_sync(
        LLMStreamDeltaEvent(
            token=msg,
            model_call_id=model_call_id,
            attempt_id=attempt_id,
            sequence=sequence,
            provisional=False,
        )
    )
    feed_output_stream(msg)


def log_llm_stream(msg: str) -> None:
    """Emit one streamed LLM token/chunk onto active Telemetry, if bound."""
    buffer = _active_buffer.get()
    if buffer is not None:
        buffer.append(msg)
        return
    _emit_llm_stream(msg)


__all__ = [
    "AttemptStreamBuffer",
    "capture_attempt_stream",
    "commit_attempt_stream",
    "discard_attempt_stream",
    "interrupt_attempt_stream",
    "log_llm_stream",
]
