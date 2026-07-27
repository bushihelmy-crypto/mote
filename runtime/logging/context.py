#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Logging trace context.

A zero-dependency leaf module that carries a ``trace_id`` through the current
async / thread context via :class:`contextvars.ContextVar`. The ``core`` logger
patcher reads :func:`current_trace_id` to stamp every log record, so any code
running inside a :func:`bind_trace` block (decorators, mixins, plain
``logger.*`` calls) is automatically traceable across concurrent agents.
"""

from __future__ import annotations

import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterator, Optional

_TRACE_ID: ContextVar[Optional[str]] = ContextVar("mote_trace_id", default=None)


def current_trace_id() -> Optional[str]:
    """Return the trace_id bound in the current context, or ``None`` if unbound."""
    return _TRACE_ID.get()


@contextmanager
def bind_trace(trace_id: Optional[str] = None) -> Iterator[str]:
    """Bind a trace_id for the duration of the ``with`` block.

    Args:
        trace_id: An explicit id to bind. A short random hex id is generated
            when omitted.

    Yields:
        The bound trace_id.
    """
    tid = trace_id or uuid.uuid4().hex[:12]
    token = _TRACE_ID.set(tid)
    try:
        yield tid
    finally:
        _TRACE_ID.reset(token)
