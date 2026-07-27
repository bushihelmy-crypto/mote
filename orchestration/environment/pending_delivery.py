#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""PendingDeliveryQueue — the plane-level buffer that makes delivery never fail.

This closes the synchronous-delivery / asynchronous-eviction impedance mismatch
that used to make a message to an evicted agent *fail* at the hard residency cap
(the sync path could not ``await`` an eviction to free a slot, so it raised
``AgentLimitReached`` and the "queue-only" fallback could not enqueue into a
not-yet-loaded runtime — the message was effectively dropped).

The fix splits delivery into two phases:

  * **synchronous accept** — the delivery path (``send_input`` /
    ``send_inter_agent_communication``) always succeeds: it either delivers
    immediately (target loaded, or a synchronous soft-reservation fit under the
    cap) or *parks* the item here. Parking is an O(1) lock-guarded append that
    never needs to free a slot, so it can never fail.
  * **asynchronous fulfilment** — the scheduler, at each turn boundary (and an
    event-driven waker for the fleet-idle case), calls back into the plane to
    drain this queue: there, in async context, it may ``await`` an LRU eviction
    to free a live-incarnation slot, rehydrate the target, and finally enqueue +
    wake. When even an async eviction cannot free room, the item simply stays
    parked and is retried next boundary — back-pressure, never loss.

A single :class:`asyncio.Event` (the *waker*) is set on every park so a
fulfilment loop blocked waiting for work is released the instant something is
queued — mirroring the per-runtime ``wake_event`` the scheduler already uses
(park-on-event, not clock polling).

The queue is guarded by a ``threading.Lock`` (same discipline as
:class:`~mote.orchestration.environment.residency.Residency`) because the synchronous
accept path and the asynchronous fulfilment loop touch it from different call
stacks. Critical sections are tiny; no I/O happens under the lock.
"""

from __future__ import annotations

import asyncio
import threading
from collections import deque
from dataclasses import dataclass
from typing import Dict, List, Optional

from mote.contracts.schema import Message
from mote.orchestration.environment.mailbox import DeliveryMode, InterAgentCommunication


@dataclass
class PendingDelivery:
    """One parked delivery awaiting a free live-incarnation slot.

    Exactly one of ``message`` / ``communication`` is set. ``communication``
    carries its own trigger-turn flag (and kind/channel metadata); a raw
    ``message`` carries an explicit :class:`DeliveryMode`.
    """

    message: Optional[Message] = None
    communication: Optional[InterAgentCommunication] = None
    mode: DeliveryMode = DeliveryMode.TRIGGER_TURN

    @property
    def is_communication(self) -> bool:
        return self.communication is not None


class PendingDeliveryQueue:
    """Per-agent FIFO of parked deliveries, plus an async waker.

    Keyed by recipient ``session_id``. The synchronous delivery path parks here
    (always succeeds); the asynchronous fulfilment loop drains here once it has
    rehydrated the target.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._queues: Dict[str, "deque[PendingDelivery]"] = {}
        # Consecutive fulfilment passes that left an agent's mail parked (no slot
        # could be freed). Reset the moment its batch is taken or dropped. Drives
        # the sustained-back-pressure observability signal — never delivery logic.
        self._back_pressure: Dict[str, int] = {}
        # Set on every park; awaited by the fulfilment waker so a fleet that is
        # otherwise fully idle still gets its parked mail delivered.
        self._waker = asyncio.Event()

    # ------------------------------------------------------------------
    # Park (synchronous, never fails)
    # ------------------------------------------------------------------
    def park(self, agent_id: str, delivery: PendingDelivery) -> None:
        """Append *delivery* for *agent_id* and signal the waker. O(1)."""
        with self._lock:
            self._queues.setdefault(agent_id, deque()).append(delivery)
        self._waker.set()

    # ------------------------------------------------------------------
    # Drain (asynchronous fulfilment side)
    # ------------------------------------------------------------------
    def agents_with_pending(self) -> List[str]:
        """Snapshot of agent ids that currently have parked deliveries."""
        with self._lock:
            return [aid for aid, q in self._queues.items() if q]

    def take_all(self, agent_id: str) -> List[PendingDelivery]:
        """Remove and return every parked delivery for *agent_id* (in order).

        The fulfilment loop takes the whole batch once it has secured a slot and
        rehydrated the target. If fulfilment then fails, the caller re-parks the
        unsent remainder (back-pressure).
        """
        with self._lock:
            q = self._queues.pop(agent_id, None)
            self._back_pressure.pop(agent_id, None)
            return list(q) if q else []

    def has_pending(self, agent_id: Optional[str] = None) -> bool:
        """True if *agent_id* (or any agent, when ``None``) has parked mail."""
        with self._lock:
            if agent_id is not None:
                q = self._queues.get(agent_id)
                return bool(q)
            return any(self._queues.values())

    def has_trigger_pending(self) -> bool:
        """True if any parked delivery would trigger a turn once fulfilled.

        This is the *outstanding-work* signal for fleet quiescence: a parked
        trigger-turn item (a raw message in ``TRIGGER_TURN`` mode, or a
        communication with ``trigger_turn`` set) is undelivered work that must
        keep the fleet non-quiescent until a slot frees up and it is fulfilled.
        A queue-only park does not, on its own, demand a turn.
        """
        with self._lock:
            for q in self._queues.values():
                for delivery in q:
                    if delivery.is_communication:
                        comm = delivery.communication
                        assert comm is not None, "is_communication delivery must carry a communication"
                        if comm.trigger_turn:
                            return True
                    elif delivery.mode is DeliveryMode.TRIGGER_TURN:
                        return True
            return False

    def drop(self, agent_id: str) -> None:
        """Forget all parked deliveries for *agent_id* (released/dead target)."""
        with self._lock:
            self._queues.pop(agent_id, None)
            self._back_pressure.pop(agent_id, None)

    def note_back_pressure(self, agent_id: str) -> int:
        """Record one more fulfilment pass that could not free a slot.

        Called by the fulfilment loop when a target stays parked (no room could
        be made this pass). Returns the running count of *consecutive* such
        passes — taking the batch (:meth:`take_all`) or dropping it
        (:meth:`drop`) resets it. Pure observability bookkeeping; it never
        influences whether or how a delivery is made.
        """
        with self._lock:
            count = self._back_pressure.get(agent_id, 0) + 1
            self._back_pressure[agent_id] = count
            return count

    # ------------------------------------------------------------------
    # Waker (event-driven fulfilment, no clock polling)
    # ------------------------------------------------------------------
    async def wait_for_pending(self) -> None:
        """Await until at least one delivery has been parked since last clear."""
        await self._waker.wait()

    def clear_waker(self) -> None:
        """Reset the waker; call right before draining so a park during the
        drain re-arms it and earns another fulfilment pass (never a lost wake)."""
        self._waker.clear()


__all__ = ["PendingDelivery", "PendingDeliveryQueue"]
