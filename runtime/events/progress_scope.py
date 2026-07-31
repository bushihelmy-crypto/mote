"""Run-scoped structured progress callback binding."""

from __future__ import annotations

import contextvars
from typing import Any, Callable

ProgressWriter = Callable[[str, Any, Any], None]
_progress_ctx: contextvars.ContextVar[ProgressWriter | None] = contextvars.ContextVar("_progress_ctx", default=None)


def current_progress_writer() -> ProgressWriter | None:
    return _progress_ctx.get()


def bind_progress_writer(writer: ProgressWriter | None) -> contextvars.Token:
    return _progress_ctx.set(writer)


def reset_progress_writer(token: contextvars.Token) -> None:
    _progress_ctx.reset(token)


__all__ = [
    "ProgressWriter",
    "bind_progress_writer",
    "current_progress_writer",
    "reset_progress_writer",
]
