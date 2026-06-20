#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Residency — LRU unload-to-disk + rehydrate-on-demand (codex ``V2Residency``).

Port of ``codex-rs/core/src/agent/control/residency.rs``. The registry caps the
*total* number of agents in a session; residency caps how many of them stay
**resident in memory** at once. When a new slot is needed and the cap is hit,
the least-recently-used *unloadable* agent is materialized to disk (via
:class:`ResidencyStore`), shut down, and dropped from the live map — freeing a
slot. Sending a message to an evicted agent later rehydrates it.

State (residents deque + pending_slots) is guarded by a ``threading.Lock`` and
only touched in short critical sections; the actual async I/O (materialize,
shutdown) runs *outside* the lock, mirroring codex.

``Residency`` does not own the live-runtime map. It is injected with:
  * ``runtime_lookup(session_id) -> Optional[AgentRuntime]`` (codex ``get_thread``),
  * ``remove_runtime(session_id)`` (sync or async; codex ``remove_thread``),
so the control plane keeps a single source of truth for liveness.
"""

from __future__ import annotations

import inspect
import threading
from collections import deque
from typing import Awaitable, Callable, Optional

from metagpt.common.events import AgentLifecycleEvent, EventBus
from metagpt.common.logs import logger
from metagpt.common.exception import AgentLimitReached
from metagpt.environment._scope import ScopedExitMixin
from metagpt.environment.runtime import AgentRuntime
from metagpt.environment.store import ResidencyStore

RuntimeLookup = Callable[[str], Optional[AgentRuntime]]
RuntimeRemover = Callable[[str], Optional[Awaitable]]


class Residency:
    """LRU residency manager keyed by ``session_id``."""

    def __init__(
        self,
        runtime_lookup: RuntimeLookup,
        *,
        store: Optional[ResidencyStore] = None,
        remove_runtime: Optional[RuntimeRemover] = None,
        event_bus: Optional[EventBus] = None,
    ):
        self._lookup = runtime_lookup
        self._store = store if store is not None else ResidencyStore()
        self._remove = remove_runtime
        self._event_bus = event_bus
        self._lock = threading.Lock()
        # LRU order: front == oldest (evict first), back == most-recently-used.
        self._residents: "deque[str]" = deque()
        self._pending_slots = 0

    @property
    def store(self) -> ResidencyStore:
        return self._store

    # ------------------------------------------------------------------
    # Reservation
    # ------------------------------------------------------------------
    async def reserve_slot(
        self,
        capacity: Optional[int],
        protected_session_id: Optional[str] = None,
    ) -> "ResidencySlot":
        """Reserve a residency slot, unloading LRU residents until one frees up.

        ``capacity`` is the in-memory ceiling (``None`` == unbounded). Raises
        :class:`AgentLimitReached` when nothing can be unloaded to make room.
        """
        while True:
            if self._try_reserve_pending_slot(capacity):
                return ResidencySlot(self)
            if not await self._try_unload_one_resident(protected_session_id):
                raise AgentLimitReached(capacity)

    def _try_reserve_pending_slot(self, capacity: Optional[int]) -> bool:
        with self._lock:
            if capacity is not None and (len(self._residents) + self._pending_slots) >= capacity:
                return False
            self._pending_slots += 1
            return True

    async def _try_unload_one_resident(self, protected_session_id: Optional[str]) -> bool:
        candidates_to_scan = self._resident_count()
        for _ in range(candidates_to_scan):
            candidate = self._pop_lru_candidate(protected_session_id)
            if candidate is None:
                return False
            runtime = self._lookup(candidate)
            if runtime is None:
                # Already gone from the live map; just drop it from residents.
                continue
            if not runtime.is_unloadable():
                # Raced into work since pop; restore as MRU and try the next one.
                self.touch(candidate)
                continue
            try:
                await self._store.materialize(runtime)
                await runtime.shutdown()
            except Exception as exc:  # noqa: BLE001 — abort this eviction, try another
                logger.warning(f"Residency: failed to unload {candidate}: {exc}")
                self.touch(candidate)
                continue
            await self._call_remove(candidate)
            if self._event_bus is not None:
                self._event_bus.emit_sync(
                    AgentLifecycleEvent(session_id=candidate, phase="evicted")
                )
            return True
        return False

    # ------------------------------------------------------------------
    # Resident bookkeeping (all short, lock-guarded)
    # ------------------------------------------------------------------
    def _resident_count(self) -> int:
        with self._lock:
            return len(self._residents)

    def _pop_lru_candidate(self, protected_session_id: Optional[str]) -> Optional[str]:
        with self._lock:
            for _ in range(len(self._residents)):
                candidate = self._residents.popleft()
                if candidate == protected_session_id:
                    self._residents.append(candidate)
                    continue
                return candidate
            return None

    def touch(self, session_id: str) -> None:
        """Mark *session_id* as most-recently-used (codex ``touch``)."""
        with self._lock:
            self._touch_locked(session_id)

    def remove(self, session_id: str) -> None:
        """Drop *session_id* from the resident set (codex ``forget``)."""
        with self._lock:
            self._discard_locked(session_id)

    def _commit_slot(self, session_id: str) -> None:
        with self._lock:
            self._pending_slots = max(0, self._pending_slots - 1)
            self._touch_locked(session_id)

    def _release_pending_slot(self) -> None:
        with self._lock:
            self._pending_slots = max(0, self._pending_slots - 1)

    # --- helpers assuming the lock is held -----------------------------
    def _touch_locked(self, session_id: str) -> None:
        self._discard_locked(session_id)
        self._residents.append(session_id)

    def _discard_locked(self, session_id: str) -> None:
        try:
            self._residents.remove(session_id)
        except ValueError:
            pass

    async def _call_remove(self, session_id: str) -> None:
        if self._remove is None:
            return
        result = self._remove(session_id)
        if inspect.isawaitable(result):
            await result

    # ------------------------------------------------------------------
    # Introspection (tests / diagnostics)
    # ------------------------------------------------------------------
    def residents(self) -> list:
        with self._lock:
            return list(self._residents)

    @property
    def pending_slots(self) -> int:
        return self._pending_slots


class ResidencySlot(ScopedExitMixin):
    """A reserved residency slot; commit it to a session or let it roll back.

    Mirrors rust's RAII ``V2ResidencySlot``: an uncommitted slot releases its
    pending reservation on drop. In Python that "drop" is either an explicit
    :meth:`rollback` or leaving a ``with``/``async with`` block.
    """

    def __init__(self, residency: Residency):
        self._residency = residency
        self._active = True

    def commit(self, session_id: str) -> None:
        """Bind the slot to *session_id*, turning the pending slot resident."""
        if self._active:
            self._residency._commit_slot(session_id)
            self._active = False

    def rollback(self) -> None:
        """Release an uncommitted slot (no-op once committed)."""
        if self._active:
            self._residency._release_pending_slot()
            self._active = False

    def _scope_exit(self) -> None:
        self.rollback()


__all__ = ["Residency", "ResidencySlot"]
