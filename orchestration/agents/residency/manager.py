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
import uuid
from collections import deque
from typing import Awaitable, Callable, Optional

from mote.contracts.agent.capacity import (
    CapacitySettlementDisposition,
    ResidentCapacityReservationReceipt,
    ResidentCapacitySettlementReceipt,
)
from mote.contracts.agent.errors import AgentLimitReached
from mote.contracts.events.agent import AgentLifecycleEvent
from mote.contracts.ports.runtime.lease import LeaseEpoch
from mote.orchestration.agents._scope import ScopedExitMixin
from mote.orchestration.agents.lifecycle.runtime import AgentRuntime
from mote.orchestration.agents.residency.lifecycle import (
    ResidentLifecyclePhase,
    ResidentLifecycleSnapshot,
    ResidentPurgeAuthorization,
    ResidentTransitionClaim,
    ResidentTransitionDisposition,
    ResidentTransitionReceipt,
)
from mote.orchestration.agents.residency.model import ResidencyIdentity
from mote.orchestration.agents.residency.store import ResidencyStore
from mote.runtime.agent.base import BaseRole
from mote.runtime.events.telemetry import TelemetryRuntime
from mote.runtime.telemetry.logging import logger

RuntimeLookup = Callable[[str], Optional[AgentRuntime]]
RuntimeRemover = Callable[[str], Optional[Awaitable]]
MaterializationAuthority = Callable[[AgentRuntime], tuple[ResidencyIdentity, LeaseEpoch]]


class Residency:
    """LRU residency manager keyed by ``session_id``."""

    def __init__(
        self,
        runtime_lookup: RuntimeLookup,
        *,
        store: ResidencyStore,
        remove_runtime: Optional[RuntimeRemover] = None,
        telemetry: Optional[TelemetryRuntime] = None,
        materialization_authority: MaterializationAuthority,
    ):
        self._lookup = runtime_lookup
        self._store = store
        self._remove = remove_runtime
        self._telemetry = telemetry
        self._materialization_authority = materialization_authority
        self._lock = threading.Lock()
        # LRU order: front == oldest (evict first), back == most-recently-used.
        self._residents: "deque[str]" = deque()
        self._pending_slots = 0
        self._lifecycle: dict[str, ResidentLifecycleSnapshot] = {}

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

    def try_reserve_sync(self, capacity: Optional[int]) -> Optional["ResidencySlot"]:
        """Synchronous soft-reservation: a slot iff under cap (no eviction).

        The synchronous twin of :meth:`reserve_slot`: it returns the same RAII
        :class:`ResidencySlot` (so both reservation paths share one commit /
        rollback discipline), but never awaits an async unload to make room.
        Callable from the synchronous delivery path (``send_input`` →
        ``_try_load_sync``) where rehydrating an evicted agent must occupy a
        live slot. Returns a fresh slot (and ``pending++``) when ``residents +
        pending < capacity`` (or ``capacity`` is ``None``); ``None`` when the
        hard cap is hit and no synchronous room can be made.
        """
        if self._try_reserve_pending_slot(capacity):
            return ResidencySlot(self)
        return None

    async def _try_unload_one_resident(self, protected_session_id: Optional[str]) -> bool:
        candidates_to_scan = self._resident_count()
        for _ in range(candidates_to_scan):
            claim = self._claim_lru_candidate(protected_session_id)
            if claim is None:
                return False
            if await self._execute_eviction(claim):
                return True
        return False

    async def retry_eviction(self, session_id: str) -> bool:
        with self._lock:
            current = self._lifecycle.get(session_id)
            if current is None or current.phase not in {
                ResidentLifecyclePhase.DRAINING,
                ResidentLifecyclePhase.EVICTION_RETRY,
            }:
                return False
            claimed = ResidentLifecycleSnapshot(
                session_id,
                current.incarnation_generation,
                current.revision + 1,
                ResidentLifecyclePhase.EVICTING,
            )
            self._lifecycle[session_id] = claimed
            claim = ResidentTransitionClaim(current, claimed)
        return await self._execute_eviction(claim)

    async def _execute_eviction(self, claim: ResidentTransitionClaim) -> bool:
        candidate = claim.claimed.agent_id
        runtime = self._lookup(candidate)
        if runtime is None:
            self.complete_eviction(claim)
            return True
        if not runtime.is_unloadable():
            self.fail_eviction(claim, draining=False)
            return False
        try:
            identity, lease = self._materialization_authority(runtime)
            if identity.incarnation_generation != claim.claimed.incarnation_generation:
                raise RuntimeError("Residency eviction incarnation generation changed")
            if isinstance(runtime.role, BaseRole):
                pin_snapshot = await runtime.role.prepare_for_eviction()
                if pin_snapshot is not None and pin_snapshot.pin_count:
                    self.fail_eviction(claim, draining=True)
                    return False
            await self._store.materialize(runtime, identity=identity, lease=lease)
            await runtime.shutdown()
        except Exception as exc:  # noqa: BLE001 — abort this eviction, try another
            logger.warning(f"Residency: failed to unload {candidate}: {exc}")
            self.fail_eviction(claim, draining=False)
            return False
        try:
            await self._call_remove(candidate)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"Residency: failed to detach {candidate}: {exc}")
            self.fail_eviction(claim, draining=False)
            return False
        receipt = self.complete_eviction(claim)
        if receipt.disposition is not ResidentTransitionDisposition.APPLIED:
            return False
        if self._telemetry is not None:
            self._telemetry.emit_sync(AgentLifecycleEvent(session_id=candidate, phase="evicted"))
        return True

    # ------------------------------------------------------------------
    # Resident bookkeeping (all short, lock-guarded)
    # ------------------------------------------------------------------
    def _resident_count(self) -> int:
        with self._lock:
            return len(self._residents)

    def _claim_lru_candidate(self, protected_session_id: Optional[str]) -> Optional[ResidentTransitionClaim]:
        with self._lock:
            for _ in range(len(self._residents)):
                candidate = self._residents.popleft()
                self._residents.append(candidate)
                if candidate == protected_session_id:
                    continue
                current = self._lifecycle.get(candidate)
                if current is None or current.phase not in {
                    ResidentLifecyclePhase.ACTIVE,
                    ResidentLifecyclePhase.DRAINING,
                    ResidentLifecyclePhase.EVICTION_RETRY,
                }:
                    continue
                claimed = ResidentLifecycleSnapshot(
                    candidate,
                    current.incarnation_generation,
                    current.revision + 1,
                    ResidentLifecyclePhase.EVICTING,
                )
                self._lifecycle[candidate] = claimed
                return ResidentTransitionClaim(current, claimed)
            return None

    def touch(self, session_id: str) -> None:
        """Mark *session_id* as most-recently-used (codex ``touch``)."""
        with self._lock:
            current = self._lifecycle.get(session_id)
            if current is None or current.phase is ResidentLifecyclePhase.ACTIVE:
                self._touch_locked(session_id)

    def register_active(
        self,
        session_id: str,
        incarnation_generation: int,
        *,
        resident: bool = True,
    ) -> None:
        if incarnation_generation < 1:
            raise ValueError("Residency incarnation generation must be positive")
        with self._lock:
            current = self._lifecycle.get(session_id)
            revision = 1 if current is None else current.revision + 1
            if current is not None and current.phase not in {
                ResidentLifecyclePhase.REHYDRATING,
                ResidentLifecyclePhase.ACTIVE,
            }:
                raise RuntimeError("Residency cannot install ACTIVE from current phase")
            self._lifecycle[session_id] = ResidentLifecycleSnapshot(
                session_id, incarnation_generation, revision, ResidentLifecyclePhase.ACTIVE
            )
            if resident:
                self._touch_locked(session_id)

    def forget_uncommitted(self, session_id: str) -> None:
        with self._lock:
            self._lifecycle.pop(session_id, None)
            self._discard_locked(session_id)

    def ensure_evicted(self, session_id: str, incarnation_generation: int) -> None:
        with self._lock:
            current = self._lifecycle.get(session_id)
            if current is None:
                self._lifecycle[session_id] = ResidentLifecycleSnapshot(
                    session_id, incarnation_generation, 1, ResidentLifecyclePhase.EVICTED
                )

    def begin_rehydration(self, session_id: str) -> ResidentTransitionClaim | None:
        with self._lock:
            current = self._lifecycle.get(session_id)
            if current is None or current.phase is not ResidentLifecyclePhase.EVICTED:
                return None
            claimed = ResidentLifecycleSnapshot(
                session_id,
                current.incarnation_generation,
                current.revision + 1,
                ResidentLifecyclePhase.REHYDRATING,
            )
            self._lifecycle[session_id] = claimed
            return ResidentTransitionClaim(current, claimed)

    def abort_rehydration(self, claim: ResidentTransitionClaim) -> ResidentTransitionReceipt:
        return self._settle_claim(
            "rehydrate",
            claim,
            ResidentLifecyclePhase.EVICTED,
            ResidentTransitionDisposition.FAILED_RETRYABLE,
        )

    def complete_rehydration(
        self, claim: ResidentTransitionClaim, *, next_generation: int
    ) -> ResidentTransitionReceipt:
        if next_generation != claim.claimed.incarnation_generation + 1:
            raise ValueError("rehydration must advance exactly one incarnation generation")
        with self._lock:
            current = self._lifecycle.get(claim.claimed.agent_id)
            if current is None or current != claim.claimed:
                return ResidentTransitionReceipt(
                    "rehydrate",
                    ResidentTransitionDisposition.STALE,
                    current or claim.claimed,
                )
            active = ResidentLifecycleSnapshot(
                current.agent_id,
                next_generation,
                current.revision + 1,
                ResidentLifecyclePhase.ACTIVE,
            )
            self._lifecycle[current.agent_id] = active
            self._touch_locked(current.agent_id)
            return ResidentTransitionReceipt("rehydrate", ResidentTransitionDisposition.APPLIED, active)

    def complete_eviction(self, claim: ResidentTransitionClaim) -> ResidentTransitionReceipt:
        receipt = self._settle_claim(
            "evict",
            claim,
            ResidentLifecyclePhase.EVICTED,
            ResidentTransitionDisposition.APPLIED,
        )
        if receipt.disposition is ResidentTransitionDisposition.APPLIED:
            with self._lock:
                self._discard_locked(claim.claimed.agent_id)
        return receipt

    def fail_eviction(self, claim: ResidentTransitionClaim, *, draining: bool) -> ResidentTransitionReceipt:
        return self._settle_claim(
            "evict",
            claim,
            ResidentLifecyclePhase.DRAINING if draining else ResidentLifecyclePhase.EVICTION_RETRY,
            (
                ResidentTransitionDisposition.PINNED_DRAINING
                if draining
                else ResidentTransitionDisposition.FAILED_RETRYABLE
            ),
        )

    def _settle_claim(
        self,
        command: str,
        claim: ResidentTransitionClaim,
        phase: ResidentLifecyclePhase,
        disposition: ResidentTransitionDisposition,
    ) -> ResidentTransitionReceipt:
        with self._lock:
            current = self._lifecycle.get(claim.claimed.agent_id)
            if current is None or current != claim.claimed:
                return ResidentTransitionReceipt(
                    command,
                    ResidentTransitionDisposition.STALE,
                    current or claim.claimed,
                )
            settled = ResidentLifecycleSnapshot(
                current.agent_id,
                current.incarnation_generation,
                current.revision + 1,
                phase,
            )
            self._lifecycle[current.agent_id] = settled
            return ResidentTransitionReceipt(command, disposition, settled)

    def runtime_for_delivery(self, session_id: str) -> Optional[AgentRuntime]:
        with self._lock:
            current = self._lifecycle.get(session_id)
            if current is None or current.phase is not ResidentLifecyclePhase.ACTIVE:
                return None
            return self._lookup(session_id)

    def deliver_if_active(
        self,
        session_id: str,
        delivery: Callable[[AgentRuntime], None],
    ) -> Optional[AgentRuntime]:
        """Validate the lifecycle fence and mutate the mailbox under one lock."""
        with self._lock:
            current = self._lifecycle.get(session_id)
            if current is None or current.phase is not ResidentLifecyclePhase.ACTIVE:
                return None
            runtime = self._lookup(session_id)
            if runtime is None:
                return None
            delivery(runtime)
            self._touch_locked(session_id)
            return runtime

    def lifecycle_snapshot(self, session_id: str) -> ResidentLifecycleSnapshot | None:
        with self._lock:
            return self._lifecycle.get(session_id)

    def mark_worker_lost(
        self, session_id: str, *, expected_generation: int, expected_revision: int
    ) -> ResidentTransitionReceipt:
        return self._command_transition(
            "worker_lost",
            session_id,
            expected_generation,
            expected_revision,
            ResidentLifecyclePhase.LOST,
            forbidden={
                ResidentLifecyclePhase.TERMINAL,
                ResidentLifecyclePhase.TOMBSTONED,
                ResidentLifecyclePhase.PURGED,
            },
        )

    def begin_termination(
        self, session_id: str, *, expected_generation: int, expected_revision: int
    ) -> ResidentTransitionReceipt:
        return self._command_transition(
            "terminate",
            session_id,
            expected_generation,
            expected_revision,
            ResidentLifecyclePhase.TERMINATING,
            forbidden={
                ResidentLifecyclePhase.TERMINAL,
                ResidentLifecyclePhase.TOMBSTONED,
                ResidentLifecyclePhase.PURGED,
            },
        )

    def complete_termination(
        self, session_id: str, *, expected_generation: int, expected_revision: int
    ) -> ResidentTransitionReceipt:
        return self._command_transition(
            "terminal",
            session_id,
            expected_generation,
            expected_revision,
            ResidentLifecyclePhase.TERMINAL,
            allowed={ResidentLifecyclePhase.TERMINATING},
        )

    def tombstone(
        self, session_id: str, *, expected_generation: int, expected_revision: int
    ) -> ResidentTransitionReceipt:
        return self._command_transition(
            "tombstone",
            session_id,
            expected_generation,
            expected_revision,
            ResidentLifecyclePhase.TOMBSTONED,
            allowed={ResidentLifecyclePhase.TERMINAL},
        )

    def purge(
        self,
        session_id: str,
        *,
        expected_generation: int,
        expected_revision: int,
        authorization: ResidentPurgeAuthorization,
    ) -> ResidentTransitionReceipt:
        current = self.lifecycle_snapshot(session_id)
        if current is None or not authorization.permits_purge:
            return ResidentTransitionReceipt(
                "purge",
                ResidentTransitionDisposition.REJECTED_GUARD,
                current
                or ResidentLifecycleSnapshot(
                    session_id,
                    expected_generation,
                    expected_revision,
                    ResidentLifecyclePhase.TOMBSTONED,
                ),
            )
        receipt = self._command_transition(
            "purge",
            session_id,
            expected_generation,
            expected_revision,
            ResidentLifecyclePhase.PURGED,
            allowed={ResidentLifecyclePhase.TOMBSTONED},
        )
        if receipt.disposition is ResidentTransitionDisposition.APPLIED:
            with self._lock:
                self._discard_locked(session_id)
        return receipt

    def _command_transition(
        self,
        command: str,
        session_id: str,
        expected_generation: int,
        expected_revision: int,
        target: ResidentLifecyclePhase,
        *,
        allowed: set[ResidentLifecyclePhase] | None = None,
        forbidden: set[ResidentLifecyclePhase] | None = None,
    ) -> ResidentTransitionReceipt:
        with self._lock:
            current = self._lifecycle.get(session_id)
            if current is None:
                missing = ResidentLifecycleSnapshot(session_id, expected_generation, expected_revision, target)
                return ResidentTransitionReceipt(command, ResidentTransitionDisposition.STALE, missing)
            if (
                current.incarnation_generation != expected_generation
                or current.revision != expected_revision
                or (allowed is not None and current.phase not in allowed)
                or (forbidden is not None and current.phase in forbidden)
            ):
                return ResidentTransitionReceipt(command, ResidentTransitionDisposition.STALE, current)
            updated = ResidentLifecycleSnapshot(session_id, expected_generation, current.revision + 1, target)
            self._lifecycle[session_id] = updated
            return ResidentTransitionReceipt(command, ResidentTransitionDisposition.APPLIED, updated)

    def remove(self, session_id: str) -> None:
        """Drop *session_id* from the resident set (codex ``forget``)."""
        with self._lock:
            self._discard_locked(session_id)

    def _commit_slot(self, session_id: str) -> None:
        with self._lock:
            self._pending_slots = max(0, self._pending_slots - 1)
            if session_id not in self._lifecycle:
                self._lifecycle[session_id] = ResidentLifecycleSnapshot(session_id, 1, 1, ResidentLifecyclePhase.ACTIVE)
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
        self._reservation_id = uuid.uuid4().hex
        self._generation = 0

    def reservation_receipt(self, incarnation_generation: int) -> ResidentCapacityReservationReceipt:
        if type(incarnation_generation) is not int or incarnation_generation < 1:
            raise ValueError("resident capacity incarnation generation must be positive")
        self._generation = incarnation_generation
        return ResidentCapacityReservationReceipt(self._reservation_id, incarnation_generation)

    def commit(self, session_id: str, incarnation_generation: int = 1) -> ResidentCapacitySettlementReceipt:
        """Bind the slot to *session_id*, turning the pending slot resident."""
        if self._active:
            self.reservation_receipt(incarnation_generation)
            self._residency._commit_slot(session_id)
            self._active = False
            return ResidentCapacitySettlementReceipt(
                self._reservation_id, incarnation_generation, CapacitySettlementDisposition.SETTLED
            )
        return ResidentCapacitySettlementReceipt(
            self._reservation_id,
            self._generation or incarnation_generation,
            CapacitySettlementDisposition.ALREADY_SETTLED,
        )

    def rollback(self) -> ResidentCapacitySettlementReceipt:
        """Release an uncommitted slot (no-op once committed)."""
        if self._active:
            self._residency._release_pending_slot()
            self._active = False
            return ResidentCapacitySettlementReceipt(
                self._reservation_id,
                self._generation,
                CapacitySettlementDisposition.SETTLED,
            )
        return ResidentCapacitySettlementReceipt(
            self._reservation_id,
            self._generation,
            CapacitySettlementDisposition.ALREADY_SETTLED,
        )

    def _scope_exit(self) -> None:
        self.rollback()


__all__ = ["Residency", "ResidencySlot"]
