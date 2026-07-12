#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""AgentExecutionLimiter — caps the number of concurrently *running* turns.

Port of ``codex-rs/core/src/agent/control/execution.rs``. Where the registry
caps the total number of agents in a session, the limiter caps how many of them
may be executing a turn at once. ``max_agents`` is initialized once and is
otherwise unbounded; a :class:`AgentExecutionGuard` increments the active count
on entry and decrements it on exit (mirroring rust's RAII ``Drop``).
"""

from __future__ import annotations

import threading
from typing import Optional

from metagpt.common.exception import AgentLimitReached
from metagpt.environment._scope import ScopedExitMixin


class AgentExecutionLimiter:
    """Counts active turns against a one-time ``max_agents`` ceiling."""

    def __init__(self):
        self._lock = threading.Lock()
        self._active = 0
        self._max_agents: Optional[int] = None  # None == uninitialized == unbounded

    def initialize(self, max_agents: int) -> None:
        """Set the ceiling once; later calls are ignored (rust ``get_or_init``)."""
        with self._lock:
            if self._max_agents is None:
                self._max_agents = max_agents

    def max_agents(self) -> Optional[int]:
        return self._max_agents

    def has_capacity(self) -> bool:
        with self._lock:
            return self._max_agents is None or self._active < self._max_agents

    def ensure_capacity(self) -> None:
        """Raise :class:`AgentLimitReached` when no execution slot is free."""
        if not self.has_capacity():
            raise AgentLimitReached(self._max_agents)

    def guard(self) -> "AgentExecutionGuard":
        with self._lock:
            self._active += 1
        return AgentExecutionGuard(self)

    def _release(self) -> None:
        with self._lock:
            self._active = max(0, self._active - 1)

    @property
    def active(self) -> int:
        return self._active


class AgentExecutionGuard(ScopedExitMixin):
    """Holds one active-turn slot until released (context manager)."""

    def __init__(self, limiter: AgentExecutionLimiter):
        self._limiter = limiter
        self._released = False

    def release(self) -> None:
        if not self._released:
            self._limiter._release()
            self._released = True

    def _scope_exit(self) -> None:
        self.release()


__all__ = ["AgentExecutionLimiter", "AgentExecutionGuard"]
