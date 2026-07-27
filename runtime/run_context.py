"""Async-task binding for the strongly typed context of the active Agent run."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Iterator

from mote.contracts.run_context import RunContext

_active_run_context: ContextVar[RunContext[Any] | None] = ContextVar("mote_run_context", default=None)


def current_run_context() -> RunContext[Any] | None:
    """Return the context bound to the current async task, if a run is active."""

    return _active_run_context.get()


@contextmanager
def bind_run_context(context: RunContext[Any]) -> Iterator[None]:
    token = _active_run_context.set(context)
    try:
        yield
    finally:
        _active_run_context.reset(token)


__all__ = ["bind_run_context", "current_run_context"]
