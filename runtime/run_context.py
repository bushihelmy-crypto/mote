"""Async-task binding for the strongly typed context of the active Agent run."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Generic, Iterator, TypeVar

from mote.kernel.execution.run_context import RunContext

DepsT = TypeVar("DepsT")


class RunContextBinding(Generic[DepsT]):
    def __init__(self) -> None:
        self._active: ContextVar[RunContext[DepsT] | None] = ContextVar("mote_run_context", default=None)

    def current(self) -> RunContext[DepsT] | None:
        return self._active.get()

    @contextmanager
    def bind(self, context: RunContext[DepsT]) -> Iterator[None]:
        token = self._active.set(context)
        try:
            yield
        finally:
            self._active.reset(token)


_active_run_context = RunContextBinding[object]()


def current_run_context() -> RunContext[object] | None:
    """Return the context bound to the current async task, if a run is active."""

    return _active_run_context.current()


@contextmanager
def bind_run_context(context: RunContext[object]) -> Iterator[None]:
    with _active_run_context.bind(context):
        yield


__all__ = ["RunContextBinding", "bind_run_context", "current_run_context"]
