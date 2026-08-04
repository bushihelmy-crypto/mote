#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""EventDrivenScheduler — drives agent turns, owns turn-atomic mailbox draining.

This is the piece that makes the control plane *event-driven*: each
:class:`AgentRuntime` gets its own asyncio "driver" task that parks on the
runtime's ``wake_event`` and, when woken, runs exactly one turn. The scheduler —
**not** ``ExecutionEngine`` — drains the mailbox at the turn boundary (between
``run()`` calls) and stages the drained messages into the role's ``msg_buffer``.

Because a turn is exactly one ``Role.run()`` and draining happens only at the
boundary, mid-turn-injected mail is naturally deferred:
  * a **trigger-turn** item sets ``wake_event`` → the driver runs another turn;
  * a **queue-only** item only sets the mailbox data event → it waits until some
    later trigger wakes the runtime, then is delivered at that boundary.

Two drive modes (don't mix them on the same scheduler instance):
  * :meth:`start` / :meth:`stop` — persistent per-runtime driver tasks (the real
    event-driven mode).
  * :meth:`run` — a bounded "barrier pump" that synchronously advances every
    ready runtime up to a bounded number of rounds.
"""

from __future__ import annotations

import asyncio
import hashlib
from contextlib import nullcontext
from typing import Awaitable, Callable, ContextManager, Dict, Optional

from mote.contracts.clock import AbsoluteInstant
from mote.contracts.conversation import Message, dump_message
from mote.contracts.ports.runtime.lease import LeaseEpoch
from mote.orchestration.agents.lifecycle.runtime import AgentRuntime
from mote.orchestration.agents.messaging.mailbox import DeliveryMode
from mote.orchestration.agents.turn_queue.limiter import AgentExecutionLimiter
from mote.orchestration.agents.turn_queue.model import (
    TurnAcceptanceRequest,
    TurnAdmissionDisposition,
    TurnMutationDisposition,
    TurnPriority,
    TurnQueueIdentity,
)
from mote.orchestration.agents.turn_queue.scheduler import DurableTurnScheduler, TurnClaimDisposition
from mote.orchestration.agents.turn_queue.scheduling import TurnSchedulingConfig
from mote.orchestration.agents.turn_queue.store import DurableTurnQueueStore
from mote.runtime.telemetry.logging import logger


class EventDrivenScheduler:
    """Schedules turns for a set of :class:`AgentRuntime` instances."""

    def __init__(
        self,
        *,
        limiter: Optional[AgentExecutionLimiter] = None,
        control_binder: Optional[Callable[[], ContextManager]] = None,
        pending_flush: Optional[Callable[[], Awaitable]] = None,
        delivery_ack: Callable[[str, tuple[str, ...]], None] | None = None,
        delivery_bind: Callable[[str, tuple[str, ...], str, str], None] | None = None,
        durable_store: DurableTurnQueueStore | None = None,
        durable_lease: LeaseEpoch | None = None,
        scheduling_config: TurnSchedulingConfig | None = None,
        now: Callable[[], AbsoluteInstant] | None = None,
        process_instance_id: str = "",
        root_owner_id: str = "",
    ):
        self._runtimes: Dict[str, AgentRuntime] = {}
        self._started = False
        # Optional concurrency cap: a guard is held around every turn so the
        # limiter's ``active`` count reflects in-flight turns (codex RAII guard).
        self._limiter = limiter
        # Optional ambient-control binder: a context manager opened around every
        # turn so a deep spawn site can discover the live plane via
        # ``current_control()`` (inherited by child asyncio tasks via context-copy).
        self._control_binder = control_binder
        # Optional plane-level pending-delivery flush: awaited at each turn
        # boundary so a message parked because its target was evicted at the hard
        # cap is fulfilled (an eviction may be awaited here) the moment capacity
        # could have improved — i.e. right after a turn frees a resident.
        self._pending_flush = pending_flush
        self._delivery_ack = delivery_ack
        self._delivery_bind = delivery_bind
        durable_values = (durable_store, durable_lease, scheduling_config, now)
        if any(value is None for value in durable_values) != all(value is None for value in durable_values):
            raise ValueError("durable turn scheduling dependencies must be complete")
        if durable_store is not None and (limiter is None or not process_instance_id):
            raise ValueError("durable turn scheduling requires limiter and process identity")
        self._durable = (
            DurableTurnScheduler(store=durable_store, limiter=limiter)
            if durable_store is not None and limiter is not None
            else None
        )
        self._durable_store = durable_store
        self._durable_lease = durable_lease
        self._scheduling_config = scheduling_config
        self._now = now
        self._process_instance_id = process_instance_id
        self._root_owner_id = root_owner_id
        if self._durable is not None and durable_store is not None and durable_lease is not None:
            for item in durable_store.load().items:
                if item.claim is not None:
                    self._durable.settle_lost_claim(item, lease=durable_lease)

    # ------------------------------------------------------------------
    # Runtime membership
    # ------------------------------------------------------------------
    def add_runtime(self, runtime: AgentRuntime) -> None:
        """Register a runtime; spawn its driver immediately if already started."""
        self._runtimes[runtime.session_id] = runtime
        if self._started:
            self._spawn_driver(runtime)

    def remove_runtime(self, session_id: str) -> None:
        self._runtimes.pop(session_id, None)

    def get_runtime(self, session_id: str) -> Optional[AgentRuntime]:
        return self._runtimes.get(session_id)

    def notify(
        self,
        session_id: str,
        message: Message,
        *,
        mode: DeliveryMode = DeliveryMode.TRIGGER_TURN,
    ) -> bool:
        runtime = self._runtimes.get(session_id)
        if runtime is None:
            return False
        runtime.mailbox.enqueue(message, mode=mode)
        if mode is DeliveryMode.TRIGGER_TURN:
            runtime.wake()
        return True

    # ------------------------------------------------------------------
    # Persistent driver mode
    # ------------------------------------------------------------------
    def start(self) -> None:
        """Spawn a persistent driver task for every registered runtime."""
        self._started = True
        for runtime in self._runtimes.values():
            self._spawn_driver(runtime)

    def _spawn_driver(self, runtime: AgentRuntime) -> None:
        if runtime.task is None or runtime.task.done():
            runtime.task = asyncio.create_task(self._driver(runtime))

    async def _driver(self, runtime: AgentRuntime) -> None:
        """Park on ``wake_event``; on each wake, drain + run one turn."""
        while not runtime.stopped:
            await runtime.wake_event.wait()
            if runtime.stopped:
                break
            # Clear BEFORE draining so a trigger-turn arriving during this turn
            # re-arms the event and earns another turn (at worst a spurious empty
            # turn — never a lost wake).
            runtime.wake_event.clear()
            batch = self._stage_and_accept(runtime)
            if batch is None:
                runtime.wake_event.set()
                await asyncio.sleep(0)
                continue
            if self._durable is None:
                delivery_ids = batch
                succeeded = await self._run_turn_safe(runtime)
                if succeeded and delivery_ids and self._delivery_ack is not None:
                    self._delivery_ack(runtime.session_id, delivery_ids)
            else:
                await self._run_durable_claim()
            # A turn just completed → a resident may now be idle/evictable, so
            # fulfil any mail parked behind the hard cap.
            await self._flush_pending()

    async def stop(self) -> None:
        """Stop persistent driving and shut down every runtime's task."""
        self._started = False
        await asyncio.gather(
            *(runtime.shutdown() for runtime in list(self._runtimes.values())),
            return_exceptions=True,
        )

    # ------------------------------------------------------------------
    # Bounded barrier pump
    # ------------------------------------------------------------------
    async def run_ready_turns(self, max_turns: int = 1) -> int:
        """Advance every ready runtime by one turn, up to *k* rounds.

        A runtime is *ready* when it has been woken (a trigger-turn was
        delivered). Stops early once no runtime is ready (quiescent). Returns the
        number of turns actually executed.
        """
        turns = 0
        for _ in range(max(0, max_turns)):
            # Fulfil parked mail first: a delivery whose target was evicted at
            # the hard cap can be rehydrated here (async eviction allowed) and
            # its runtime woken, so it becomes ready within this same budget.
            await self._flush_pending()
            ready = [rt for rt in self._runtimes.values() if self._is_ready(rt)]
            if not ready:
                break
            for runtime in ready:
                runtime.wake_event.clear()
                batch = self._stage_and_accept(runtime)
                if batch is None:
                    runtime.wake_event.set()
                    continue
                if self._durable is None:
                    succeeded = await self._run_turn_safe(runtime)
                    if succeeded and batch and self._delivery_ack is not None:
                        self._delivery_ack(runtime.session_id, batch)
                else:
                    await self._run_durable_claim()
                turns += 1
        return turns

    # ------------------------------------------------------------------
    # Quiescence
    # ------------------------------------------------------------------
    def quiescent(self) -> bool:
        """True when no runtime is running or has pending trigger-turn work."""
        for runtime in self._runtimes.values():
            if runtime.active_turn:
                return False
            if self._is_ready(runtime):
                return False
        return True

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    @staticmethod
    def _is_ready(runtime: AgentRuntime) -> bool:
        return runtime.wake_event.is_set() or runtime.mailbox.has_trigger_turn()

    @staticmethod
    def _stage_mailbox(runtime: AgentRuntime) -> tuple[str, ...]:
        """Drain the mailbox at the turn boundary into the role's msg_buffer."""
        messages, delivery_ids = runtime.mailbox.drain_for_processing()
        for message in messages:
            runtime.msg_buffer.push(message)
        return delivery_ids

    def _stage_and_accept(self, runtime: AgentRuntime) -> tuple[str, ...] | None:
        messages, delivery_ids = runtime.mailbox.drain_for_processing()
        if not messages:
            return ()
        if self._durable_store is not None:
            path = runtime.agent_path.as_str() if runtime.agent_path is not None else "/root"
            root_id = self._root_owner_id or runtime.session_id
            request_id = (
                "turn_" + hashlib.sha256((runtime.session_id + "\0" + "\0".join(delivery_ids)).encode()).hexdigest()
            )
            payload_digest = hashlib.sha256(
                "\0".join(
                    hashlib.sha256(dump_message(message).encode("utf-8")).hexdigest() for message in messages
                ).encode()
            ).hexdigest()
            assert self._now is not None and self._scheduling_config is not None
            assert self._durable_lease is not None
            receipt = self._durable_store.prepare_acceptance(
                TurnAcceptanceRequest(
                    TurnQueueIdentity(
                        self._durable_store.load().queue_id,
                        request_id,
                        root_id,
                        path,
                        runtime.session_id,
                        delivery_ids,
                    ),
                    self._scheduling_config.generation,
                    TurnPriority.NORMAL,
                    self._now(),
                    None,
                    3,
                    payload_digest,
                ),
                lease=self._durable_lease,
            )
            if receipt.disposition not in {
                TurnAdmissionDisposition.ACCEPTED,
                TurnAdmissionDisposition.DUPLICATE,
            }:
                runtime.mailbox.restore_processing(messages, delivery_ids)
                return None
            if receipt.revision is None:
                runtime.mailbox.restore_processing(messages, delivery_ids)
                return None
            if self._delivery_bind is not None:
                self._delivery_bind(runtime.session_id, delivery_ids, request_id, payload_digest)
            committed = self._durable_store.commit_acceptance(
                request_id=request_id,
                expected_item_revision=receipt.revision,
                lease=self._durable_lease,
            )
            if committed.disposition is not TurnMutationDisposition.APPLIED:
                runtime.mailbox.restore_processing(messages, delivery_ids)
                return None
        for message in messages:
            runtime.msg_buffer.push(message)
        return delivery_ids

    async def _run_durable_claim(self) -> None:
        assert self._durable is not None
        assert self._durable_lease is not None
        assert self._scheduling_config is not None
        assert self._now is not None
        attempt = self._durable.claim_next(
            config=self._scheduling_config,
            now=self._now(),
            lease=self._durable_lease,
            process_instance_id=self._process_instance_id,
        )
        if attempt.disposition is not TurnClaimDisposition.CLAIMED or attempt.claim is None:
            return
        claim = attempt.claim
        runtime = self._runtimes.get(claim.item.identity.agent_id)
        if runtime is None:
            self._durable.settle(claim, succeeded=False, reason="agent_not_resident", lease=self._durable_lease)
            return
        try:
            succeeded = await self._run_turn_safe(runtime, acquire_permit=False)
        except asyncio.CancelledError:
            self._durable.settle(
                claim,
                succeeded=False,
                reason="turn_cancelled",
                lease=self._durable_lease,
            )
            raise
        delivery_ack = self._delivery_ack
        receipt = self._durable.settle(
            claim,
            succeeded=succeeded,
            reason="turn_completed" if succeeded else "turn_failed",
            lease=self._durable_lease,
            acknowledge=(
                None
                if not succeeded or delivery_ack is None
                else lambda: delivery_ack(runtime.session_id, claim.item.identity.delivery_ids)
            ),
        )

    def cancel_agent_turns(self, agent_id: str) -> None:
        if self._durable is not None and self._durable_lease is not None:
            self._durable.cancel_agent(
                agent_id,
                reason="subtree_cancellation",
                lease=self._durable_lease,
            )

    def ensure_driver(self, runtime: AgentRuntime) -> None:
        """Re-spawn a runtime's driver task if it has exited (e.g. post-interrupt)."""
        if self._started and not runtime.stopped:
            self._spawn_driver(runtime)

    async def _flush_pending(self) -> None:
        """Run the injected pending-delivery flush (best-effort, never fatal)."""
        if self._pending_flush is None:
            return
        try:
            await self._pending_flush()
        except Exception as exc:  # noqa: BLE001 — keep driving
            logger.warning(f"Scheduler: pending-delivery flush failed: {exc}")

    async def _run_turn_safe(self, runtime: AgentRuntime, *, acquire_permit: bool = True) -> bool:
        guard = await self._limiter.acquire() if self._limiter is not None and acquire_permit else None
        binder = self._control_binder() if self._control_binder is not None else nullcontext()
        try:
            with binder:
                await runtime.run_one_turn()
            return True
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — status already ERRORED; keep driving
            logger.warning(f"Scheduler: turn for {runtime.session_id} errored: {exc}")
            return False
        finally:
            if guard is not None:
                guard.release()


__all__ = ["EventDrivenScheduler"]
