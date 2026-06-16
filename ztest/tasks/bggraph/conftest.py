#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Shared fixtures / helpers for the ``metagpt.executor.bggraph`` test suite.

Nodes are built with the :func:`sync_node` / :func:`gated_node` factories so a
test can declare a graph in a couple of lines.  Everything is offline and
deterministic — ``asyncio.Event`` gates drive ordering instead of ``sleep``.
"""
from __future__ import annotations

import asyncio
from typing import Callable

from metagpt.executor.tasks.bggraph import GraphState, Stage


class S(GraphState):
    """Generic test state with a single integer input ``x``."""

    x: int = 0


def sync_node(fn: Callable[[GraphState], object]):
    """Build a node whose result is ``fn(state)`` (no poll phase)."""

    async def node(state):
        async def submit():
            return fn(state)

        return Stage(submit=submit())

    return node


def gated_node(event: asyncio.Event, fn: Callable[[GraphState], object]):
    """Build a node that blocks on *event* before returning ``fn(state)``."""

    async def node(state):
        async def submit():
            await event.wait()
            return fn(state)

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


def flaky_node(fail_times: int, value, counter: list):
    """Build a node that raises ``fail_times`` then returns *value*.

    ``counter`` accumulates one entry per attempt.
    Uses ``ConnectionError`` which is retryable by ``is_retryable``.
    """

    async def node(state):
        async def submit():
            counter.append(1)
            if len(counter) <= fail_times:
                raise ConnectionError(f"flaky {len(counter)}")
            return value

        return Stage(submit=submit())

    return node


def non_retryable_flaky_node(fail_times: int, value, counter: list):
    """Build a node that raises a non-retryable error ``fail_times`` then returns *value*.

    ``counter`` accumulates one entry per attempt.
    Uses ``ValueError`` which is NOT retryable by ``is_retryable``.
    """

    async def node(state):
        async def submit():
            counter.append(1)
            if len(counter) <= fail_times:
                raise ValueError(f"non-retryable flaky {len(counter)}")
            return value

        return Stage(submit=submit())

    return node
