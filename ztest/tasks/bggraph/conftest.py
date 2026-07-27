#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Shared fixtures / helpers for the ``mote.runtime.tools.bggraph`` test suite.

Nodes are built with the :func:`sync_node` / :func:`gated_node` factories so a
test can declare a graph in a couple of lines.  Everything is offline and
deterministic — ``asyncio.Event`` gates drive ordering instead of ``sleep``.
"""
from __future__ import annotations

import asyncio
from typing import Callable

from mote.orchestration.tasks.bggraph import GraphState, Stage


class S(GraphState):
    """Generic test state with a single integer input ``x``."""

    x: int = 0


def _wrap(field, result):
    """Wrap a node's raw result as a ``{field: result}`` update dict.

    With the field/channel state model a node returns a dict of field updates.
    Passing ``field=<node name>`` keeps ``state.<node>`` readable as before
    (via ``extra="allow"``), so existing routers / downstream reads still work.
    When ``field`` is ``None`` the raw result is returned unchanged (callers that
    already return a dict, or nodes that never execute).
    """
    return {field: result} if field is not None else result


def sync_node(fn: Callable[[GraphState], object], *, field: str | None = None):
    """Build a node whose result is ``fn(state)`` (no poll phase).

    If *field* is given, the result is wrapped as ``{field: fn(state)}``.
    """

    async def node(state):
        async def submit():
            return _wrap(field, fn(state))

        return Stage(submit=submit())

    return node


def gated_node(event: asyncio.Event, fn: Callable[[GraphState], object], *, field: str | None = None):
    """Build a node that blocks on *event* before returning ``fn(state)``."""

    async def node(state):
        async def submit():
            await event.wait()
            return _wrap(field, fn(state))

        return Stage(submit=submit())

    return node


def boom_node(exc: BaseException, *, attempts_holder: list | None = None):
    """Build a node that always raises *exc* (optionally counting attempts)."""

    async def node(state):
        async def submit():
            if attempts_holder is not None:
                attempts_holder.append(1)
            raise exc

        return Stage(submit=submit())

    return node


def flaky_node(fail_times: int, value, counter: list, *, field: str | None = None):
    """Build a node that raises ``fail_times`` then returns *value*.

    ``counter`` accumulates one entry per attempt.
    Uses ``ConnectionError`` which is retryable by ``is_retryable``.
    """

    async def node(state):
        async def submit():
            counter.append(1)
            if len(counter) <= fail_times:
                raise ConnectionError(f"flaky {len(counter)}")
            return _wrap(field, value)

        return Stage(submit=submit())

    return node


def non_retryable_flaky_node(fail_times: int, value, counter: list, *, field: str | None = None):
    """Build a node that raises a non-retryable error ``fail_times`` then returns *value*.

    ``counter`` accumulates one entry per attempt.
    Uses ``ValueError`` which is NOT retryable by ``is_retryable``.
    """

    async def node(state):
        async def submit():
            counter.append(1)
            if len(counter) <= fail_times:
                raise ValueError(f"non-retryable flaky {len(counter)}")
            return _wrap(field, value)

        return Stage(submit=submit())

    return node
