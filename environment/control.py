#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""AgentControl — the session-scoped multi-agent control plane.

Port of the consumer-facing surface of ``codex-rs/core/src/agent/control.rs``
(the codex-specific spawn/rollout/shell-snapshot machinery is intentionally
omitted — this round is "plane infrastructure only"). ``AgentControl`` ties the
five primitives together and owns the single source of truth for liveness:

  * :class:`AgentRegistry`  — total-agent cap + path/nickname index,
  * :class:`AgentExecutionLimiter` — concurrent-turn cap,
  * :class:`Residency`      — LRU unload-to-disk + rehydrate,
  * :class:`EventDrivenScheduler` — per-agent turn driving + mailbox draining,
  * :class:`ResidencyStore` — on-disk materialization.

The live runtime map (``session_id -> AgentRuntime``) lives here; the scheduler
and residency read it through this object (residency via injected callbacks).

Delivery (``send_input`` / ``send_inter_agent_communication``):
  ensure execution capacity (trigger-turn only) → rehydrate the target if it was
  evicted to disk → enqueue into its mailbox → wake it (trigger-turn) or not
  (queue-only). A completion watcher delivers a **queue-only** notification to a
  parent agent when its child reaches a final status.
"""

from __future__ import annotations

import asyncio
import weakref
from typing import Callable, Dict, Optional

from metagpt.common.logs import logger
from metagpt.common.schema import Message, UserMessage
from metagpt.environment.agent_path import AgentPath
from metagpt.common.exception import AgentNotFound, AgentNotKnown
from metagpt.environment.limiter import AgentExecutionLimiter
from metagpt.environment.mailbox import DeliveryMode, InterAgentCommunication
from metagpt.environment.registry import AgentMetadata, AgentRegistry
from metagpt.environment.residency import Residency
from metagpt.environment.runtime import AgentRuntime, AgentStatus, is_final
from metagpt.environment.scheduler import EventDrivenScheduler
from metagpt.environment.store import ResidencyStore


def format_completion_notification(reference: str, status: AgentStatus) -> str:
    """Render the parent-facing message announcing a child's terminal status."""
    return f"Agent '{reference}' finished with status: {status.value}"


class AgentControl:
    """Control-plane handle shared by every agent in one session tree."""

    def __init__(
        self,
        *,
        session_id: Optional[str] = None,
        store: Optional[ResidencyStore] = None,
        max_threads: Optional[int] = None,
        residency_capacity: Optional[int] = None,
        role_loader: Optional[Callable[[dict], object]] = None,
        watch_interval: float = 0.01,
    ):
        self.session_id = session_id
        self._runtimes: Dict[str, AgentRuntime] = {}
        self._registry = AgentRegistry()
        self._limiter = AgentExecutionLimiter()
        if max_threads is not None:
            self._limiter.initialize(max_threads)
        self._store = store if store is not None else ResidencyStore()
        self._scheduler = EventDrivenScheduler(limiter=self._limiter)
        self._residency = Residency(
            self._runtimes.get,
            store=self._store,
            remove_runtime=self._remove_runtime,
        )
        self._residency_capacity = residency_capacity
        self._role_loader = role_loader
        self._watch_interval = watch_interval
        self._watchers: list[asyncio.Task] = []

    # ------------------------------------------------------------------
    # Accessors
    # ------------------------------------------------------------------
    @property
    def registry(self) -> AgentRegistry:
        return self._registry

    @property
    def limiter(self) -> AgentExecutionLimiter:
        return self._limiter

    @property
    def residency(self) -> Residency:
        return self._residency

    @property
    def scheduler(self) -> EventDrivenScheduler:
        return self._scheduler

    @property
    def store(self) -> ResidencyStore:
        return self._store

    def runtimes(self) -> Dict[str, AgentRuntime]:
        return dict(self._runtimes)

    def get_runtime(self, agent_id: str) -> Optional[AgentRuntime]:
        return self._runtimes.get(agent_id)

    # ------------------------------------------------------------------
    # Membership
    # ------------------------------------------------------------------
    def add_agent(
        self,
        runtime: AgentRuntime,
        *,
        metadata: Optional[AgentMetadata] = None,
        root: bool = False,
    ) -> AgentRuntime:
        """Register a live runtime into the plane (map + scheduler + registry)."""
        session_id = runtime.session_id
        self._runtimes[session_id] = runtime
        self._scheduler.add_runtime(runtime)
        if root:
            self._registry.register_root_thread(session_id)
        elif metadata is not None:
            metadata.agent_id = session_id
            self._registry._register_spawned_thread(metadata)
        self._residency.touch(session_id)
        return runtime

    def register_session_root(self, current_thread_id: str, current_parent_thread_id: Optional[str] = None) -> None:
        """Index the root thread iff it has no parent (codex ``register_session_root``)."""
        if current_parent_thread_id is None:
            self._registry.register_root_thread(current_thread_id)

    def _remove_runtime(self, session_id: str) -> None:
        """Drop a runtime from the live map + scheduler (residency eviction)."""
        self._runtimes.pop(session_id, None)
        self._scheduler.remove_runtime(session_id)

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------
    def get_status(self, agent_id: str) -> AgentStatus:
        runtime = self._runtimes.get(agent_id)
        if runtime is None:
            # Evicted-to-disk agents are "not found" while unloaded.
            return AgentStatus.NOT_FOUND
        return runtime.status

    # ------------------------------------------------------------------
    # Reference resolution (path / nickname / session_id -> session_id)
    # ------------------------------------------------------------------
    def resolve_agent_reference(self, agent_reference: str, *, current_path: Optional[AgentPath] = None) -> str:
        """Resolve a reference to a live ``session_id`` (codex ``resolve_agent_reference``).

        Tries, in order: a direct ``session_id`` in the live map, an absolute or
        ``current_path``-relative :class:`AgentPath`, then a nickname. Raises
        :class:`AgentNotKnown` when nothing matches.
        """
        if agent_reference in self._runtimes:
            return agent_reference

        base = current_path if current_path is not None else AgentPath.root()
        try:
            resolved = base.resolve(agent_reference)
        except Exception:  # noqa: BLE001 — not a path-shaped reference
            resolved = None
        if resolved is not None:
            session_id = self._registry.agent_id_for_path(resolved)
            if session_id is not None:
                return session_id

        meta = self._registry.agent_metadata_for_nickname(agent_reference)
        if meta is not None and meta.agent_id is not None:
            return meta.agent_id

        raise AgentNotKnown(agent_reference)

    # ------------------------------------------------------------------
    # Delivery
    # ------------------------------------------------------------------
    def send_input(
        self,
        agent_id: str,
        message: Message,
        *,
        mode: DeliveryMode = DeliveryMode.TRIGGER_TURN,
    ) -> AgentRuntime:
        """Deliver a raw message to an agent (rehydrating it first if evicted)."""
        runtime = self._ensure_loaded(agent_id)
        trigger = mode is DeliveryMode.TRIGGER_TURN
        if trigger:
            self._limiter.ensure_capacity()
        runtime.mailbox.enqueue(message, mode=mode)
        if trigger:
            runtime.wake()
        self._residency.touch(agent_id)
        self._record_last_task_message(agent_id, _preview(message))
        return runtime

    def send_inter_agent_communication(
        self,
        agent_id: str,
        communication: InterAgentCommunication,
    ) -> AgentRuntime:
        """Deliver a structured agent->agent communication (codex ``Op::InterAgentCommunication``)."""
        runtime = self._ensure_loaded(agent_id)
        if communication.trigger_turn:
            self._limiter.ensure_capacity()
        runtime.mailbox.enqueue_communication(communication)
        if communication.trigger_turn:
            runtime.wake()
        self._residency.touch(agent_id)
        self._record_last_task_message(agent_id, communication.content)
        return runtime

    async def interrupt(self, agent_id: str) -> AgentStatus:
        """Best-effort interrupt of an agent's in-flight turn.

        Cancels the runtime's current driver task if it is mid-turn; the turn's
        ``CancelledError`` path sets ``INTERRUPTED``. A fresh driver is re-spawned
        when the scheduler is in persistent mode so the agent stays drivable.
        """
        runtime = self._runtimes.get(agent_id)
        if runtime is None:
            return AgentStatus.NOT_FOUND
        task = runtime.task
        if task is not None and not task.done() and runtime.active_turn:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:  # noqa: BLE001
                pass
            runtime.task = None
            self._scheduler.ensure_driver(runtime)
        elif not is_final(runtime.status):
            runtime.status = AgentStatus.INTERRUPTED
        return runtime.status

    # ------------------------------------------------------------------
    # Rehydration
    # ------------------------------------------------------------------
    def _ensure_loaded(self, agent_id: str) -> AgentRuntime:
        """Return the live runtime, rehydrating from disk if it was evicted."""
        runtime = self._runtimes.get(agent_id)
        if runtime is not None:
            return runtime
        if not self._store.has(agent_id):
            raise AgentNotFound(agent_id)
        restored = self._store.rehydrate(agent_id, role_loader=self._role_loader)
        if restored is None:
            raise AgentNotFound(agent_id)
        self._runtimes[agent_id] = restored
        self._scheduler.add_runtime(restored)
        self._residency.touch(agent_id)
        self._store.forget(agent_id)
        logger.info(f"AgentControl: rehydrated agent {agent_id}")
        return restored

    # ------------------------------------------------------------------
    # Completion watcher
    # ------------------------------------------------------------------
    def start_completion_watcher(
        self,
        child_id: str,
        parent_id: str,
        *,
        child_path: Optional[AgentPath] = None,
        parent_path: Optional[AgentPath] = None,
        child_reference: Optional[str] = None,
    ) -> asyncio.Task:
        """Notify *parent_id* (queue-only) once *child_id* reaches a final status.

        The closure holds a :class:`weakref` to ``self`` (codex ``Weak`` handle):
        if the control plane is dropped, the watcher bails instead of keeping it
        alive.
        """
        weak_self = weakref.ref(self)
        interval = self._watch_interval
        reference = child_reference or (child_path.name() if child_path is not None else child_id)

        async def _watch() -> None:
            while True:
                ctrl = weak_self()
                if ctrl is None:
                    return
                child = ctrl._runtimes.get(child_id)
                if child is None:
                    return  # evicted/removed before completion
                if is_final(child.status) and not child.active_turn and child.mailbox.empty():
                    status = child.status
                    break
                del ctrl, child
                await asyncio.sleep(interval)

            ctrl = weak_self()
            if ctrl is None:
                return
            message = format_completion_notification(reference, status)
            communication = InterAgentCommunication.new(
                author=child_path or AgentPath.root(),
                recipient=parent_path or AgentPath.root(),
                content=message,
                trigger_turn=False,  # queue-only: parent sees it at its next boundary
            )
            try:
                ctrl.send_inter_agent_communication(parent_id, communication)
            except Exception as exc:  # noqa: BLE001 — parent may be gone
                logger.warning(f"AgentControl: completion notify to {parent_id} failed: {exc}")

        task = asyncio.create_task(_watch())
        self._watchers.append(task)
        return task

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def start(self) -> None:
        self._scheduler.start()

    async def stop(self) -> None:
        for task in self._watchers:
            if not task.done():
                task.cancel()
        self._watchers.clear()
        await self._scheduler.stop()

    async def run(self, k: int = 1) -> int:
        return await self._scheduler.run(k)

    def quiescent(self) -> bool:
        return self._scheduler.quiescent()

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    def _record_last_task_message(self, agent_id: str, text: str) -> None:
        if text:
            self._registry.update_last_task_message(agent_id, text)
        else:
            self._registry.clear_last_task_message(agent_id)


def _preview(message: Message) -> str:
    content = getattr(message, "content", "")
    return content if isinstance(content, str) else ""


__all__ = ["AgentControl", "format_completion_notification"]
