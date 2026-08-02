#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""AgentExecutionLimiter — caps the number of concurrently *running* turns.

Port of ``codex-rs/core/src/agent/control/execution.rs``. Where the registry
caps the total number of agents in a session, the limiter caps how many of them
may be executing a turn at once. ``max_concurrent_turns`` is initialized once and is
otherwise unbounded; a :class:`AgentExecutionGuard` increments the active count
on entry and decrements it on exit (mirroring rust's RAII ``Drop``).
"""

from __future__ import annotations

import asyncio
import threading
import uuid
from collections import deque
from typing import Deque, Optional

from mote.contracts.agent.capacity import (
    CapacitySettlementDisposition,
    TurnCapacityPermitReceipt,
    TurnCapacitySettlementReceipt,
)
from mote.contracts.agent.errors import AgentLimitReached
from mote.orchestration.agents._scope import ScopedExitMixin


class AgentExecutionLimiter:
    """Counts active turns against a one-time concurrent-turn ceiling."""

    def __init__(self):
        self._lock = threading.Lock()
        self._max_concurrent_turns: Optional[int] = None
        self._permits: set[str] = set()
        self._settled: set[str] = set()
        self._waiters: Deque[asyncio.Future[AgentExecutionGuard]] = deque()

    def initialize(self, max_concurrent_turns: int) -> None:
        """Set the ceiling once; later calls are ignored (rust ``get_or_init``)."""
        if type(max_concurrent_turns) is not int or max_concurrent_turns <= 0:
            raise ValueError("max_concurrent_turns must be a positive integer")
        with self._lock:
            if self._max_concurrent_turns is None:
                self._max_concurrent_turns = max_concurrent_turns

    def max_concurrent_turns(self) -> Optional[int]:
        return self._max_concurrent_turns

    def has_capacity(self) -> bool:
        """Return an observational snapshot; never grants execution authority."""
        with self._lock:
            return self._has_capacity_locked()

    def ensure_capacity(self) -> None:
        """Raise :class:`AgentLimitReached` when no execution slot is free."""
        if not self.has_capacity():
            raise AgentLimitReached(message="concurrent Agent turn capacity is exhausted")

    def guard(self) -> "AgentExecutionGuard":
        """Atomically acquire now or fail without incrementing the active set."""
        with self._lock:
            if not self._has_capacity_locked():
                raise AgentLimitReached(message="concurrent Agent turn capacity is exhausted")
            return self._issue_locked()

    async def acquire(self) -> "AgentExecutionGuard":
        """Wait for and atomically acquire one execution permit."""
        loop = asyncio.get_running_loop()
        with self._lock:
            if self._has_capacity_locked():
                return self._issue_locked()
            waiter: asyncio.Future[AgentExecutionGuard] = loop.create_future()
            self._waiters.append(waiter)
        try:
            return await asyncio.shield(waiter)
        except asyncio.CancelledError:
            guard = waiter.result() if waiter.done() and not waiter.cancelled() else None
            if guard is not None:
                guard.release()
            else:
                removed = False
                with self._lock:
                    try:
                        self._waiters.remove(waiter)
                        removed = True
                    except ValueError:
                        pass
                if not removed:
                    waiter.add_done_callback(lambda completed: completed.result().release())
            raise

    def _has_capacity_locked(self) -> bool:
        return self._max_concurrent_turns is None or len(self._permits) < self._max_concurrent_turns

    def _issue_locked(self) -> "AgentExecutionGuard":
        receipt = TurnCapacityPermitReceipt(uuid.uuid4().hex)
        self._permits.add(receipt.permit_id)
        return AgentExecutionGuard(self, receipt)

    def _release(self, receipt: TurnCapacityPermitReceipt) -> TurnCapacitySettlementReceipt:
        wake: tuple[asyncio.AbstractEventLoop, asyncio.Future[AgentExecutionGuard], AgentExecutionGuard] | None = None
        with self._lock:
            if receipt.permit_id not in self._permits:
                disposition = (
                    CapacitySettlementDisposition.ALREADY_SETTLED
                    if receipt.permit_id in self._settled
                    else CapacitySettlementDisposition.NOT_FOUND
                )
                return TurnCapacitySettlementReceipt(receipt.permit_id, disposition)
            self._permits.remove(receipt.permit_id)
            self._settled.add(receipt.permit_id)
            while self._waiters and self._has_capacity_locked():
                waiter = self._waiters.popleft()
                if waiter.cancelled():
                    continue
                guard = self._issue_locked()
                wake = (waiter.get_loop(), waiter, guard)
                break
        if wake is not None:
            loop, waiter, guard = wake
            loop.call_soon_threadsafe(waiter.set_result, guard)
        return TurnCapacitySettlementReceipt(receipt.permit_id, CapacitySettlementDisposition.SETTLED)

    @property
    def active(self) -> int:
        with self._lock:
            return len(self._permits)


class AgentExecutionGuard(ScopedExitMixin):
    """Holds one active-turn slot until released (context manager)."""

    def __init__(self, limiter: AgentExecutionLimiter, receipt: TurnCapacityPermitReceipt):
        self._limiter = limiter
        self.receipt = receipt
        self._released = False

    def release(self) -> TurnCapacitySettlementReceipt:
        if not self._released:
            settlement = self._limiter._release(self.receipt)
            self._released = True
            return settlement
        return TurnCapacitySettlementReceipt(self.receipt.permit_id, CapacitySettlementDisposition.ALREADY_SETTLED)

    def _scope_exit(self) -> None:
        self.release()


__all__ = ["AgentExecutionLimiter", "AgentExecutionGuard"]
