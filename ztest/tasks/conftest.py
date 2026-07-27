#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Shared fixtures and helpers for the ``mote.tasks`` test suite.

The fixtures keep every test fully offline and deterministic:

- ``msg_buffer`` is a real :class:`~mote.contracts.schema.MessageQueue` (it
  builds offline and is what the pool pushes notifications into).
- ``pool`` is a real :class:`~mote.tasks.BackgroundTaskPool` bound to that
  buffer.
- The coroutine helpers (``echo`` / ``boom`` / ``gated`` / ``forever``) are
  *factories* — each call returns a fresh, single-use coroutine so a test can
  submit several without reusing an already-awaited one.

Tasks are driven with ``asyncio.Event`` gates rather than ``sleep`` so the
event-loop ordering is explicit and the suite never races on wall-clock time.
"""
from __future__ import annotations

import asyncio
from typing import Optional

import pytest

from mote.contracts.schema import MessageQueue
from mote.orchestration.tasks import BackgroundTaskPool

# ---------------------------------------------------------------------------
# Coroutine factories
# ---------------------------------------------------------------------------


async def echo(value="ok"):
    """Return *value* immediately."""
    return value


async def boom(exc: Optional[BaseException] = None):
    """Raise *exc* (defaults to ``ValueError('boom')``)."""
    raise exc or ValueError("boom")


async def gated(event: asyncio.Event, value="done"):
    """Block on *event* then return *value*."""
    await event.wait()
    return value


async def started_gated(started: asyncio.Event, release: asyncio.Event, value="done"):
    """Signal *started* (semaphore acquired / running) then block on *release*."""
    started.set()
    await release.wait()
    return value


async def forever():
    """Block forever (until cancelled)."""
    await asyncio.Event().wait()


async def wait_started(event: asyncio.Event, timeout: float = 1.0) -> None:
    """Await *event* with a safety timeout so a hang fails fast."""
    await asyncio.wait_for(event.wait(), timeout=timeout)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def msg_buffer() -> MessageQueue:
    return MessageQueue()


@pytest.fixture
def pool(msg_buffer) -> BackgroundTaskPool:
    """A pool that pushes completion notifications straight into the buffer.

    The pool delivers directly to ``msg_buffer`` (no Telemetry round-trip), so
    completions land in the buffer just as they do in production.
    """
    return BackgroundTaskPool(msg_buffer)
