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
from contextlib import nullcontext
from typing import Awaitable, Callable, ContextManager, Dict, Optional

from mote.orchestration.agents.execution.limiter import AgentExecutionLimiter
from mote.orchestration.agents.lifecycle.runtime import AgentRuntime
from mote.orchestration.agents.messaging.mailbox import DeliveryMode
from mote.runtime.telemetry.logging import logger


class EventDrivenScheduler:
    """Schedules turns for a set of :class:`AgentRuntime` instances."""

    def __init__(
        self,
        *,
        limiter: Optional[AgentExecutionLimiter] = None,
        control_binder: Optional[Callable[[], ContextManager]] = None,
        pending_flush: Optional[Callable[[], Awaitable]] = None,
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

    # ------------------------------------------------------------------
    # Delivery (enqueue + optional wake)
    # ------------------------------------------------------------------
    def notify(
        self,
        session_id: str,
        message,
        *,
        mode: DeliveryMode = DeliveryMode.TRIGGER_TURN,
    ) -> bool:
        """Enqueue *message* into a runtime's mailbox, waking it on trigger-turn.

        Returns ``False`` if the session is unknown (caller may rehydrate first).
        """
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
            self._stage_mailbox(runtime)
            await self._run_turn_safe(runtime)
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
                self._stage_mailbox(runtime)
                await self._run_turn_safe(runtime)
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
    def _stage_mailbox(runtime: AgentRuntime) -> None:
        """Drain the mailbox at the turn boundary into the role's msg_buffer."""
        for message in runtime.mailbox.drain_for_turn():
            runtime.msg_buffer.push(message)

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

    async def _run_turn_safe(self, runtime: AgentRuntime) -> None:
        guard = self._limiter.guard() if self._limiter is not None else None
        binder = self._control_binder() if self._control_binder is not None else nullcontext()
        try:
            with binder:
                await runtime.run_one_turn()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — status already ERRORED; keep driving
            logger.warning(f"Scheduler: turn for {runtime.session_id} errored: {exc}")
        finally:
            if guard is not None:
                guard.release()


__all__ = ["EventDrivenScheduler"]
