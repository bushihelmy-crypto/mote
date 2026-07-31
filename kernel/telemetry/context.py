"""Run-scoped trace identity propagation with no logging backend."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterator

TraceIdProvider = Callable[[], str | None]

_trace_id_provider: ContextVar[TraceIdProvider | None] = ContextVar("mote_kernel_trace_id_provider", default=None)


def current_trace_id() -> str | None:
    provider = _trace_id_provider.get()
    return provider() if provider is not None else None


@contextmanager
def bind_trace_id_provider(provider: TraceIdProvider | None) -> Iterator[None]:
    token = _trace_id_provider.set(provider)
    try:
        yield
    finally:
        _trace_id_provider.reset(token)


__all__ = ["bind_trace_id_provider", "current_trace_id"]
