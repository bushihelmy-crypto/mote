"""Run-scoped structured progress callback binding."""

from __future__ import annotations

import contextvars

from mote.contracts.task.progress import ProgressEventSink

_progress_ctx: contextvars.ContextVar[ProgressEventSink | None] = contextvars.ContextVar("_progress_ctx", default=None)


def current_progress_sink() -> ProgressEventSink | None:
    return _progress_ctx.get()


def bind_progress_sink(sink: ProgressEventSink | None) -> contextvars.Token:
    return _progress_ctx.set(sink)


def reset_progress_sink(token: contextvars.Token) -> None:
    _progress_ctx.reset(token)


__all__ = [
    "bind_progress_sink",
    "current_progress_sink",
    "reset_progress_sink",
]
