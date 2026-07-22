"""Ambient identity of the tool call currently crossing the executor."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterator

_tool_call_id: ContextVar[str | None] = ContextVar("mote_tool_call_id", default=None)


def current_tool_call_id() -> str | None:
    return _tool_call_id.get()


@contextmanager
def bind_tool_call_id(call_id: str | None) -> Iterator[None]:
    token = _tool_call_id.set(call_id)
    try:
        yield
    finally:
        _tool_call_id.reset(token)
