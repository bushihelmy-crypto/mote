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
import re
import uuid
import weakref
from typing import Callable, Dict, Optional, TypeVar

from mote.contracts.agent import (
    AgentConstructionRequest,
    ContextPolicy,
    Lifecycle,
    RunnableAgent,
    SpawnContext,
    SpawnPlan,
)
from mote.contracts.agent.policy import SpawnIntent
from mote.contracts.conversation import Message, UserMessage
from mote.contracts.events.agent import AgentLifecycleEvent
from mote.contracts.ports.agent.spawn_policy import SpawnPolicyExtensionSpec
from mote.contracts.ports.agent.team_roster import TeamRosterMember
from mote.contracts.ports.events.telemetry import TelemetryIdentity, TelemetryOverflow, TelemetrySubscriptionSpec
from mote.orchestration.agents.execution.limiter import AgentExecutionLimiter
from mote.orchestration.agents.execution.turn_scheduler import EventDrivenScheduler
from mote.orchestration.agents.identity.path import AgentPath
from mote.orchestration.agents.identity.registry import AgentMetadata, AgentRegistry, next_agent_spawn_depth
from mote.orchestration.agents.lifecycle.admission import build_spawn_admission_policy
from mote.orchestration.agents.lifecycle.handle import ChildAgentHandle
from mote.orchestration.agents.lifecycle.runtime import AgentRuntime, AgentStatus, is_final
from mote.orchestration.agents.lifecycle.spawn import SpawnPhase, SpawnTransaction
from mote.orchestration.agents.messaging.mailbox import DeliveryMode, InterAgentCommunication
from mote.orchestration.agents.messaging.pending import PendingDelivery, PendingDeliveryQueue
from mote.orchestration.agents.messaging.routing import CommGraph, CommKind
from mote.orchestration.agents.residency.manager import Residency, ResidencySlot
from mote.orchestration.agents.residency.store import ResidencyStore
from mote.runtime.agent.base import BaseRole
from mote.runtime.agent.control import set_control
from mote.runtime.agent.incarnation import AgentIncarnationFactory
from mote.runtime.errors import AgentLimitReached, AgentNotFound, AgentNotKnown
from mote.runtime.events.log_subscriber import LogSubscriber
from mote.runtime.events.telemetry import TelemetryBinding, TelemetryManifest, TelemetryRuntime, TelemetryState
from mote.runtime.models.cost.node import CostNode
from mote.runtime.models.cost.tracker import CostTracker
from mote.runtime.telemetry.logging import logger

# Consecutive fulfilment passes a parked delivery may sit through before its
# sustained back-pressure is surfaced as an AgentLifecycleEvent (and then once
# per multiple thereafter). Tuned for "a few boundaries is normal churn, a
# steady wall of them is a starving target worth a log line".
_DELIVERY_STUCK_FLUSHES = 5
OutputT = TypeVar("OutputT")


def _path_depth(path: AgentPath) -> int:
    """Depth of *path* below root: ``/root`` is 0, ``/root/a`` is 1, ..."""
    return len(path.as_str().strip("/").split("/")) - 1


def _sanitize_segment(name: str) -> str:
    """Coerce *name* into a valid :class:`AgentPath` segment (``[a-z0-9_]``)."""
    segment = re.sub(r"[^a-z0-9_]", "_", (name or "").lower()).strip("_")
    return segment or "agent"


def format_completion_notification(reference: str, status: AgentStatus) -> str:
    """Render the parent-facing message announcing a child's terminal status."""
    return f"Agent '{reference}' finished with status: {status.value}"


class AgentControl:
    """Control-plane handle shared by every agent in one session tree."""

    def __init__(
        self,
        *,
        store: ResidencyStore,
        session_id: Optional[str] = None,
        max_agents: Optional[int] = None,
        max_depth: Optional[int] = None,
        max_cost_usd: Optional[float] = None,
        max_total_tokens: Optional[int] = None,
        spawn_policy_extensions: tuple[SpawnPolicyExtensionSpec, ...] = (),
        residency_capacity: Optional[int] = None,
        role_loader: Callable[[dict[str, object]], object] | None = None,
        incarnation_factory: AgentIncarnationFactory | None = None,
        watch_interval: float = 0.01,
    ):
        self.session_id = session_id
        self._runtimes: Dict[str, AgentRuntime] = {}
        self._registry = AgentRegistry()
        self._limiter = AgentExecutionLimiter()
        # Total-agent spawn cap (registry) + concurrent-turn cap (limiter) share
        # one ceiling, mirroring codex's single ``max_threads``. ``None`` == no cap.
        self._max_agents = max_agents
        # Admission limits are immutable composition inputs read by the sealed
        # SpawnAdmissionPolicy for every resolved spawn intent.
        self._max_depth = max_depth
        self._max_cost_usd = max_cost_usd
        self._max_total_tokens = max_total_tokens
        self._spawn_policy = build_spawn_admission_policy(spawn_policy_extensions)
        if max_agents is not None:
            self._limiter.initialize(max_agents)
        # The orchestration plane owns a process-local, loss-tolerant telemetry
        # runtime because it lives outside every per-Role observation scope.
        self._telemetry = TelemetryRuntime(
            TelemetryManifest(
                (
                    TelemetryBinding(
                        TelemetrySubscriptionSpec(
                            identity=TelemetryIdentity("mote.orchestration.agent_lifecycle_log"),
                            capacity=1024,
                            overflow=TelemetryOverflow.DROP_OLDEST,
                        ),
                        LogSubscriber(),
                    ),
                )
            )
        )
        self._store = store
        # Bind self as the ambient plane around every turn so a deep spawn site
        # discovers it via ``current_control()`` (inherited by child tasks).
        self._scheduler = EventDrivenScheduler(
            limiter=self._limiter,
            control_binder=lambda: set_control(self),
            pending_flush=self._flush_pending_deliveries,
        )
        self._residency = Residency(
            self._runtimes.get,
            store=self._store,
            remove_runtime=self._remove_runtime,
            telemetry=self._telemetry,
        )
        # Live-incarnation cap is enforced by residency, not the registry: the
        # registry counts identities (which persist across eviction), residency
        # counts agents resident in memory. ``max_agents`` becomes the residency
        # ceiling unless an explicit ``residency_capacity`` is given.
        self._residency_capacity = residency_capacity if residency_capacity is not None else max_agents
        self._role_loader = role_loader
        self._incarnation_factory = incarnation_factory or AgentIncarnationFactory()
        self._watch_interval = watch_interval
        self._watchers: list[asyncio.Task[None]] = []
        # Fleet cost mirror tree: one CostNode per agent (its own tracker as the
        # node bucket + parent pointer), so per-node attribution is preserved and
        # subtree totals are computed on demand.
        self._cost_nodes: Dict[str, CostNode] = {}
        self._cost_root: Optional[CostNode] = None
        # Communication graph: address routing + named channels + subtree
        # queries, orthogonal to the lineage tree (registry).
        self._comm_graph = CommGraph()
        # Plane-level delivery buffer: a message that cannot be delivered
        # synchronously (target evicted + hard residency/execution cap) is parked
        # here and fulfilled asynchronously at the next turn boundary (where an
        # eviction may be awaited). Delivery therefore never fails and never
        # drops — it is at worst deferred (back-pressure).
        self._pending = PendingDeliveryQueue()
        # The event-driven fulfilment waker task (started with the scheduler).
        self._pending_waker_task: asyncio.Task[None] | None = None

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

    @property
    def telemetry(self) -> TelemetryRuntime:
        """The process-local observation plane for agent lifecycle telemetry."""
        return self._telemetry

    @property
    def cost_root(self) -> Optional[CostNode]:
        """The fleet cost mirror tree root (``None`` until a root agent is added)."""
        return self._cost_root

    def cost_node_for(self, agent_id: str) -> Optional[CostNode]:
        """The cost node for *agent_id*, or ``None`` if it has no dedicated node."""
        return self._cost_nodes.get(agent_id)

    @property
    def comm_graph(self) -> CommGraph:
        """The communication graph (address routing + named channels + subtree)."""
        return self._comm_graph

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
        self._register_incarnation(runtime.role, session_id)
        self._runtimes[session_id] = runtime
        self._scheduler.add_runtime(runtime)
        if root:
            self.register_session_root(session_id)
            self._register_cost_root(runtime, session_id)
            self._comm_graph.register(session_id, agent_path=AgentPath.root())
        elif metadata is not None:
            metadata.agent_id = session_id
            self._registry.register_spawned_agent(metadata)
            self._comm_graph.register(session_id, agent_path=metadata.agent_path)
        self._residency.touch(session_id)
        self._telemetry.emit_sync(
            AgentLifecycleEvent(session_id=session_id, phase="added", detail=type(runtime.role).__name__)
        )
        return runtime

    def register_session_root(self, current_agent_id: str, current_parent_agent_id: Optional[str] = None) -> None:
        """Index the root agent iff it has no parent (codex ``register_session_root``).

        The single entry point for root-agent indexing: ``add_agent(root=True)``
        routes through here too, so both share the underlying
        ``register_root_agent`` call.
        """
        if current_parent_agent_id is None:
            self._registry.register_root_agent(current_agent_id)

    def _remove_runtime(self, session_id: str) -> None:
        """Drop a runtime from the live map + scheduler (residency eviction)."""
        self._runtimes.pop(session_id, None)
        self._scheduler.remove_runtime(session_id)

    def _register_incarnation(self, role: object, session_id: str) -> None:
        """Retain a construction-only blueprint across in-process eviction."""

        if isinstance(role, BaseRole):
            self._incarnation_factory.register(
                session_id,
                role.incarnation_blueprint(),
            )

    # ------------------------------------------------------------------
    # Cost mirror tree
    # ------------------------------------------------------------------
    @staticmethod
    def _role_cost_tracker(
        role: RunnableAgent[OutputT],
    ) -> Optional[CostTracker]:
        """Read a role's cost bucket through its public attribution seam."""
        tracker = role.spawn_cost_tracker()
        return tracker if isinstance(tracker, CostTracker) else None

    def _register_cost_root(self, runtime: AgentRuntime, agent_id: str) -> None:
        """Seed the cost tree root from the root agent's own tracker."""
        tracker = self._role_cost_tracker(runtime.role)
        if tracker is None:
            return
        node = CostNode(tracker=tracker, agent_path=AgentPath.ROOT, agent_id=agent_id)
        self._cost_root = node
        self._cost_nodes[agent_id] = node

    def _add_cost_node(
        self,
        role: RunnableAgent[OutputT],
        parent_id: Optional[str],
        agent_id: str,
        child_path: AgentPath,
    ) -> None:
        """Adopt the child's own tracker as a node under its parent's node.

        A skill_fork shares the parent's context (and thus its tracker); in that
        case no separate node is created so the shared bucket is not double-counted.
        """
        parent_node = self._cost_nodes.get(parent_id) if parent_id else None
        if parent_node is None:
            parent_node = self._cost_root
        child_tracker = self._role_cost_tracker(role)
        if child_tracker is None:
            return
        if parent_node is not None and child_tracker is parent_node.tracker:
            return  # shared context (skill_fork): same bucket, no separate node
        node = CostNode(
            tracker=child_tracker,
            parent=parent_node,
            agent_path=child_path.as_str(),
            agent_id=agent_id,
        )
        if parent_node is not None:
            parent_node.children.append(node)
        self._cost_nodes[agent_id] = node

    def _remove_cost_node(self, agent_id: str) -> None:
        node = self._cost_nodes.pop(agent_id, None)
        if node is not None and node.parent is not None:
            try:
                node.parent.children.remove(node)
            except ValueError:
                pass

    async def _cancel_spawn_watcher(self, task: asyncio.Task[None]) -> None:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        try:
            self._watchers.remove(task)
        except ValueError:
            pass

    # ------------------------------------------------------------------
    # Spawn authority — the single birth channel for every child agent
    # ------------------------------------------------------------------
    async def spawn_agent(self, spec: SpawnPlan[OutputT]) -> ChildAgentHandle[OutputT]:
        """Spawn one child agent through the single authority (codex ``spawn_agent_internal``).

        The only place caps, depth, lineage path, nickname, cost node, and
        residency are enforced. Sequence: resolve parent + depth check → reserve
        a live-incarnation slot (residency cap) → reserve an identity spawn slot
        (registry bookkeeping) → reserve nickname + path → build the role via the
        factory (inside the reservation, so any failure auto-rolls-back path +
        nickname + identity + residency slot) → add a cost node under the parent
        → wrap in a runtime → commit → register (MANAGED → scheduler + completion
        watcher + resident; EPHEMERAL → inline, slot held by the handle).

        Raises :class:`AgentLimitReached` when the cap or depth limit is hit.
        """
        transaction = SpawnTransaction()
        parent_path = self._resolve_parent_path(spec.parent_id)
        child_depth = next_agent_spawn_depth(_path_depth(parent_path))
        max_depth = spec.max_depth if spec.max_depth is not None else self._max_depth
        decision = await self._spawn_policy.process(
            SpawnIntent(
                parent_path=parent_path.as_str(),
                child_depth=child_depth,
                max_depth=max_depth,
                fleet_cost_usd=(self._cost_root.subtree_cost() if self._cost_root else 0.0),
                max_cost_usd=self._max_cost_usd,
                fleet_total_tokens=(self._cost_root.subtree_usage().total_tokens if self._cost_root else 0),
                max_total_tokens=self._max_total_tokens,
                agent_role=spec.agent_role or "",
                nickname=spec.nickname or "",
            )
        )
        if not decision.accepted:
            raise AgentLimitReached(message=decision.reason or "spawn denied")
        transaction.advance(SpawnPhase.ADMITTED)

        # Live-incarnation cap: residency reserves a slot, evicting the LRU idle
        # resident if full (raises AgentLimitReached when nothing can free room).
        slot = await self._residency.reserve_slot(self._residency_capacity, protected_session_id=spec.parent_id)
        transaction.advance(SpawnPhase.RESIDENCY_RESERVED, slot.rollback)
        try:
            # Identity bookkeeping only (no cap here — identities persist across
            # eviction; the live ceiling is the residency slot above).
            reservation = self._registry.reserve_spawn_slot(None)
            transaction.advance(SpawnPhase.IDENTITY_RESERVED, reservation.rollback)
            base = _sanitize_segment(spec.nickname or spec.agent_role or "agent")
            segment = f"{base}_{uuid.uuid4().hex[:8]}"
            nickname = reservation.reserve_agent_nickname_with_preference([base], preferred=segment)
            child_path = parent_path.join(segment)
            reservation.reserve_agent_path(child_path)

            spawn_ctx = self._build_spawn_context(spec, child_path)
            request = AgentConstructionRequest(
                parent_session_id=spec.parent_id,
                child_identity=segment,
                child_path=child_path.as_str(),
                nickname=nickname,
                cwd=spawn_ctx.cwd,
                context_policy=spec.context_policy,
                spawn_context=spawn_ctx,
            )
            role = spec.definition.builder.build(request)
            transaction.advance(SpawnPhase.CHILD_BUILT, role.cleanup)
            self._provision_context(role, spec, spawn_ctx)
            role.bind_agent_control(self)
            transaction.advance(SpawnPhase.PROVISIONED)

            runtime = AgentRuntime(role, agent_path=child_path)
            agent_id = runtime.session_id
            self._register_incarnation(role, agent_id)
            transaction.own(lambda: self._incarnation_factory.unregister(agent_id))
            self._add_cost_node(role, spec.parent_id, agent_id, child_path)
            transaction.own(lambda: self._remove_cost_node(agent_id))

            # Install all supervision while the child remains absent from the
            # identity registry. create_task cannot run until this synchronous
            # commit section yields, so no watcher observes partial state.
            self._runtimes[agent_id] = runtime
            transaction.own(lambda: self._remove_runtime(agent_id))
            self._comm_graph.register(agent_id, agent_path=child_path)
            transaction.own(lambda: self._comm_graph.remove(agent_id))
            transaction.advance(SpawnPhase.REGISTERED_INERT)
            if spec.lifecycle is Lifecycle.MANAGED:
                self._scheduler.add_runtime(runtime)
                if spec.watch_completion and spec.parent_id:
                    watcher = self.start_completion_watcher(agent_id, spec.parent_id, child_path=child_path)
                    transaction.own(lambda: self._cancel_spawn_watcher(watcher))
                if spec.timeout_seconds is not None:
                    watchdog = self.start_ttl_watchdog(agent_id, spec.timeout_seconds)
                    transaction.own(lambda: self._cancel_spawn_watcher(watchdog))
            transaction.advance(SpawnPhase.SUPERVISED)

            reservation.commit(
                AgentMetadata(
                    agent_id=agent_id,
                    agent_path=child_path,
                    agent_nickname=nickname,
                    agent_role=spec.agent_role,
                )
            )
            transaction.own(lambda: self._registry.release_spawned_agent(agent_id))
            if spec.lifecycle is Lifecycle.MANAGED:
                slot.commit(agent_id)
                transaction.own(lambda: self._residency.remove(agent_id))
            transaction.commit()
        except BaseException:
            await transaction.rollback_shielded()
            raise

        self._telemetry.emit_sync(AgentLifecycleEvent(session_id=agent_id, phase="added", detail=type(role).__name__))
        if spec.lifecycle is Lifecycle.MANAGED:
            return ChildAgentHandle(runtime, control=self, agent_id=agent_id, agent_path=child_path)
        # EPHEMERAL: caller runs it inline via the handle; never enters the
        # scheduler. It still occupies a live slot (held pending, not evictable)
        # — the handle releases it on aclose. Any ``timeout_seconds`` bounds that
        # single inline turn (the handle wraps its await in ``asyncio.wait_for``).
        return ChildAgentHandle(
            runtime,
            control=self,
            agent_id=agent_id,
            agent_path=child_path,
            residency_slot=slot,
            timeout_seconds=spec.timeout_seconds,
        )

    def release_child(self, agent_id: str) -> None:
        """Release a spawned child: drop from map/scheduler/residency + free the cap slot."""
        self._remove_runtime(agent_id)
        self._residency.remove(agent_id)
        self._registry.release_spawned_agent(agent_id)
        self._comm_graph.remove(agent_id)
        self._pending.drop(agent_id)  # any parked mail for a released agent is moot
        self._incarnation_factory.unregister(agent_id)

    def _resolve_parent_path(self, parent_id: Optional[str]) -> AgentPath:
        """The parent's registered path, or root when unknown/unparented."""
        if parent_id:
            meta = self._registry.agent_metadata_for_id(parent_id)
            if meta is not None and meta.agent_path is not None:
                return meta.agent_path
        return AgentPath.root()

    def _build_spawn_context(self, spec: SpawnPlan[OutputT], child_path: AgentPath) -> SpawnContext:
        """Gather the parent's cwd / config / cost tracker for the child factory.

        ``parent_cost_tracker`` is optional context (a skill_fork shares the
        parent's context directly); the cost mirror tree does not rely on it.
        """
        parent_rt = self._runtimes.get(spec.parent_id) if spec.parent_id else None
        if parent_rt is not None:
            return parent_rt.role.build_child_spawn_context(parent_id=spec.parent_id, agent_path=child_path)
        return SpawnContext(
            parent_id=spec.parent_id,
            agent_path=child_path,
            parent_session_id=spec.parent_id or "",
        )

    def _provision_context(
        self,
        role: RunnableAgent[OutputT],
        spec: SpawnPlan[OutputT],
        spawn_ctx: SpawnContext,
    ) -> None:
        """Give the freshly-built child role its LLM Context, per the spawn policy.

        The single place a spawned child's context is set — unconditionally, so a
        factory can never (accidentally or otherwise) own this invariant. Runs
        before the cost node is added, since that node adopts
        ``role._context.cost_manager``.

        ``FRESH`` builds an independent :class:`Context` from the spawn's config,
        giving the child its own :class:`CostTracker` (a distinct cost-tree node).
        ``SHARE_PARENT`` hands over the parent's own Context (fork-like spawns):
        the shared tracker is deduped by :meth:`_add_cost_node`. SHARE_PARENT with
        no live parent is a wiring bug — surfaced loudly, never silently patched.
        """
        parent_rt = self._runtimes.get(spec.parent_id) if spec.parent_id else None
        if parent_rt is None:
            if spec.context_policy is ContextPolicy.SHARE_PARENT:
                raise RuntimeError(f"SHARE_PARENT spawn requires live parent '{spec.parent_id}'")
            role.provision_unparented_spawn(spawn_ctx)
            return
        parent_rt.role.provision_spawned_child(role, spec.context_policy)

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------
    def get_status(self, agent_id: str) -> AgentStatus:
        runtime = self._runtimes.get(agent_id)
        if runtime is None:
            # Evicted-to-disk agents are "not found" while unloaded.
            return AgentStatus.NOT_FOUND
        return runtime.status

    def team_members(self, session_id: str) -> tuple[TeamRosterMember, ...]:
        """Return the parent, siblings, and direct children of one Agent."""
        own = self._registry.agent_metadata_for_id(session_id)
        if own is None or own.agent_path is None:
            return ()
        own_path = own.agent_path
        parent_path = own_path.parent()
        members: list[TeamRosterMember] = []
        if parent_path is not None:
            parent_id = self._registry.agent_id_for_path(parent_path)
            parent = self._registry.agent_metadata_for_id(parent_id) if parent_id else None
            if parent is not None:
                members.append(self._team_member("parent", parent))
        for metadata in self._registry.live_agents():
            if metadata.agent_id == session_id or metadata.agent_path is None:
                continue
            metadata_parent = metadata.agent_path.parent()
            if parent_path is not None and metadata_parent == parent_path:
                members.append(self._team_member("sibling", metadata))
            elif metadata_parent == own_path:
                members.append(self._team_member("child", metadata))
        return tuple(members)

    def _team_member(self, relation: str, metadata: AgentMetadata) -> TeamRosterMember:
        session_id = metadata.agent_id or ""
        name = metadata.agent_nickname or (metadata.agent_path.name() if metadata.agent_path is not None else "")
        return TeamRosterMember(
            relation=relation,
            name=name,
            role=metadata.agent_role or "",
            session_id=session_id,
            status=self.get_status(session_id).value if session_id else "",
        )

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
    def dispatch_automation(self, target: str, content: str) -> Optional[AgentRuntime]:
        """Map an automation command onto the public agent delivery surface."""
        return self.send_input(
            target,
            UserMessage(content=content),
            mode=DeliveryMode.TRIGGER_TURN,
        )

    def send_input(
        self,
        agent_id: str,
        message: Message,
        *,
        mode: DeliveryMode = DeliveryMode.TRIGGER_TURN,
    ) -> Optional[AgentRuntime]:
        """Deliver a raw message to an agent, or park it for async fulfilment.

        Never fails and never drops: if the target is evicted and the hard
        residency cap blocks a synchronous reservation — or a trigger-turn item
        arrives while the execution cap is exhausted — the message is parked in
        the plane-level :class:`PendingDeliveryQueue` and delivered at the next
        turn boundary (where an eviction may be awaited to free room). Raises
        :class:`AgentNotFound` only when *agent_id* is genuinely unknown.

        Returns the live runtime when delivered immediately, or ``None`` when the
        message was parked because its target could not be loaded synchronously.
        """
        runtime = self._try_load_sync(agent_id)
        trigger = mode is DeliveryMode.TRIGGER_TURN
        if runtime is None or (trigger and not self._limiter.has_capacity()):
            self._pending.park(agent_id, PendingDelivery(message=message, mode=mode))
            return runtime
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
    ) -> Optional[AgentRuntime]:
        """Deliver a structured agent->agent communication, or park it.

        Same never-fail / never-drop contract as :meth:`send_input`: a target
        that cannot be loaded synchronously (hard cap) — or a trigger-turn
        communication arriving while the execution cap is exhausted — is parked
        and fulfilled asynchronously at the next boundary.
        """
        runtime = self._try_load_sync(agent_id)
        trigger = communication.trigger_turn
        if runtime is None or (trigger and not self._limiter.has_capacity()):
            self._pending.park(agent_id, PendingDelivery(communication=communication))
            return runtime
        runtime.mailbox.enqueue_communication(communication)
        if trigger:
            runtime.wake()
        self._residency.touch(agent_id)
        self._record_last_task_message(agent_id, communication.content)
        return runtime

    # ------------------------------------------------------------------
    # Communication graph — named channels + subtree broadcast
    # ------------------------------------------------------------------
    def send_to_channel(
        self,
        channel: str,
        message: Message,
        *,
        mode: DeliveryMode = DeliveryMode.TRIGGER_TURN,
    ) -> list[str]:
        """Fan *message* out to every agent that has joined *channel*.

        Returns the list of session ids the message was accepted for (delivered
        immediately or parked for asynchronous fulfilment — delivery never
        fails or drops). A member that is genuinely unknown is skipped.
        """
        accepted: list[str] = []
        for session_id in self._comm_graph.channel_members(channel):
            try:
                self.send_input(session_id, message, mode=mode)
            except AgentNotFound:
                continue
            accepted.append(session_id)
        return accepted

    def broadcast_subtree(
        self,
        root_id: str,
        communication: InterAgentCommunication,
        *,
        include_root: bool = False,
    ) -> list[str]:
        """Deliver *communication* to every agent in *root_id*'s lineage subtree.

        Resolves the subtree by the root agent's :class:`AgentPath` (defaulting to
        ``include_root=False`` so a parent broadcasting to its descendants does not
        message itself). Returns the session ids the communication was accepted
        for (delivered or parked); a genuinely unknown member is skipped.
        """
        root_path = self._comm_graph.path_for(root_id)
        if root_path is None:
            return []
        accepted: list[str] = []
        for session_id in self._comm_graph.subtree_members(root_path, include_root=include_root):
            try:
                self.send_inter_agent_communication(session_id, communication)
            except AgentNotFound:
                continue
            accepted.append(session_id)
        return accepted

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
            except Exception as exc:  # noqa: BLE001 — driver crashed on interrupt
                logger.debug(f"AgentControl: driver task raised on interrupt: {exc}")
            runtime.task = None
            self._scheduler.ensure_driver(runtime)
        elif not is_final(runtime.status):
            runtime.status = AgentStatus.INTERRUPTED
        self._telemetry.emit_sync(
            AgentLifecycleEvent(session_id=agent_id, phase="interrupted", detail=runtime.status.value)
        )
        return runtime.status

    # ------------------------------------------------------------------
    # Rehydration
    # ------------------------------------------------------------------
    def _try_load_sync(self, agent_id: str) -> Optional[AgentRuntime]:
        """Return the live runtime, rehydrating *synchronously* if possible.

        Fast, non-raising loader for the synchronous delivery path. If the agent
        is already loaded it is returned immediately. If it was evicted to disk
        it is rehydrated only when a synchronous soft-reservation
        (:meth:`Residency.try_reserve_sync`, which never awaits an eviction) fits
        under the live-incarnation cap. At the hard cap it returns ``None`` (the
        caller parks the message for asynchronous fulfilment). Raises
        :class:`AgentNotFound` only when the agent is unknown on disk too.
        """
        runtime = self._runtimes.get(agent_id)
        if runtime is not None:
            return runtime
        if not self._store.has(agent_id):
            raise AgentNotFound(agent_id)
        slot = self._residency.try_reserve_sync(self._residency_capacity)
        if slot is None:
            return None  # hard cap, no synchronous room — caller parks
        return self._install_rehydrated(agent_id, slot)

    async def _ensure_loaded_async(self, agent_id: str) -> Optional[AgentRuntime]:
        """Return the live runtime, rehydrating via an *evicting* reservation.

        The asynchronous fulfilment loader: unlike :meth:`_try_load_sync` it may
        ``await`` an LRU eviction (:meth:`Residency.reserve_slot`) to free a
        live-incarnation slot at the hard cap. Returns ``None`` when even an
        eviction cannot free room (every resident busy / protected) — the caller
        leaves the deliveries parked and retries next boundary (back-pressure).
        Raises :class:`AgentNotFound` only when the agent is unknown.
        """
        runtime = self._runtimes.get(agent_id)
        if runtime is not None:
            return runtime
        if not self._store.has(agent_id):
            raise AgentNotFound(agent_id)
        try:
            slot = await self._residency.reserve_slot(self._residency_capacity)
        except AgentLimitReached:
            return None  # back-pressure: nothing evictable right now
        # The evicting reservation already holds the freed slot — install on it
        # directly (no rollback-and-re-reserve dance, so no transient window in
        # which a racing reservation could steal the room).
        return self._install_rehydrated(agent_id, slot)

    def _install_rehydrated(self, agent_id: str, slot: ResidencySlot) -> Optional[AgentRuntime]:
        """Rehydrate *agent_id* from disk onto an already-held *slot*.

        Shared body of both loaders: materialize the runtime, register it into
        the live map + scheduler, commit the slot (pending -> resident), and drop
        the on-disk copy. Rolls the slot back (and returns ``None`` / re-raises)
        on any failure, so a reserved slot is never leaked — both loaders hand in
        a :class:`ResidencySlot`, so commit / rollback follow one discipline.
        """
        try:
            loader = self._role_loader or self._incarnation_factory.loader(agent_id)
            restored = self._store.rehydrate(agent_id, role_loader=loader)
        except BaseException:
            slot.rollback()
            raise
        if restored is None:
            slot.rollback()
            raise AgentNotFound(agent_id)
        restored.role.bind_agent_control(self)
        metadata = self._registry.agent_metadata_for_id(agent_id)
        if metadata is not None:
            restored.agent_path = metadata.agent_path
        self._runtimes[agent_id] = restored
        self._scheduler.add_runtime(restored)
        slot.commit(agent_id)  # pending -> resident (evictable)
        self._store.forget(agent_id)
        self._telemetry.emit_sync(AgentLifecycleEvent(session_id=agent_id, phase="rehydrated"))
        return restored

    # ------------------------------------------------------------------
    # Pending-delivery fulfilment (asynchronous, at turn boundaries)
    # ------------------------------------------------------------------
    async def _flush_pending_deliveries(self) -> int:
        """Deliver every parked message whose target can now be loaded.

        Called by the scheduler at each turn boundary (and by the event-driven
        waker). For each agent with parked mail it secures a live slot via the
        *evicting* async loader, then drains and enqueues the whole batch. When a
        target cannot be loaded (hard cap, nothing evictable) or has vanished its
        deliveries are left parked / dropped respectively. Returns the number of
        deliveries actually flushed.
        """
        flushed = 0
        for agent_id in self._pending.agents_with_pending():
            try:
                runtime = await self._ensure_loaded_async(agent_id)
            except AgentNotFound:
                self._pending.drop(agent_id)  # target gone for good
                continue
            if runtime is None:
                self._note_delivery_back_pressure(agent_id)  # still no room, leave parked
                continue
            for delivery in self._pending.take_all(agent_id):
                self._deliver_now(runtime, agent_id, delivery)
                flushed += 1
        return flushed

    def _note_delivery_back_pressure(self, agent_id: str) -> None:
        """Track a parked target that could not be loaded this pass, and surface
        *sustained* back-pressure as an :class:`AgentLifecycleEvent`.

        Pure observability: it reuses runtime Telemetry + its log handler already in
        place (no metrics subsystem) and never touches delivery semantics. The
        first stuck pass is normal churn, so we stay silent until the count
        crosses :data:`_DELIVERY_STUCK_FLUSHES`, then emit once per threshold
        multiple to flag a target that is starving (every resident busy /
        protected, nothing evictable) without spamming a line each boundary.
        """
        stuck = self._pending.note_back_pressure(agent_id)
        if stuck >= _DELIVERY_STUCK_FLUSHES and stuck % _DELIVERY_STUCK_FLUSHES == 0:
            self._telemetry.emit_sync(
                AgentLifecycleEvent(
                    session_id=agent_id,
                    phase="delivery_back_pressure",
                    detail=f"parked {stuck} flushes (no live-incarnation slot)",
                )
            )

    def _deliver_now(self, runtime: AgentRuntime, agent_id: str, delivery: PendingDelivery) -> None:
        """Enqueue an already-loaded parked *delivery* into *runtime*'s mailbox."""
        if delivery.is_communication:
            comm = delivery.communication
            assert comm is not None, "is_communication delivery must carry a communication"
            runtime.mailbox.enqueue_communication(comm)
            if comm.trigger_turn:
                runtime.wake()
            self._record_last_task_message(agent_id, comm.content)
        else:
            message = delivery.message
            assert message is not None, "message delivery must carry a message"
            runtime.mailbox.enqueue(message, mode=delivery.mode)
            if delivery.mode is DeliveryMode.TRIGGER_TURN:
                runtime.wake()
            self._record_last_task_message(agent_id, _preview(message))
        self._residency.touch(agent_id)

    async def _pending_waker_loop(self) -> None:
        """Event-driven fulfilment for the fleet-idle case.

        When no turn is running, the scheduler's per-boundary flush never fires;
        this loop parks on the queue's waker (set on every :meth:`park`) and runs
        a flush the instant something is parked — mirroring the scheduler's own
        park-on-event driver (no clock polling).
        """
        while True:
            await self._pending.wait_for_pending()
            self._pending.clear_waker()  # clear before flush so a park during it re-arms
            try:
                await self._flush_pending_deliveries()
            except Exception as exc:  # noqa: BLE001 — keep the waker alive
                logger.warning(f"AgentControl: pending-delivery flush failed: {exc}")

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
    ) -> asyncio.Task[None]:
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
                kind=CommKind.NOTIFICATION,
            )
            try:
                ctrl.send_inter_agent_communication(parent_id, communication)
            except Exception as exc:  # noqa: BLE001 — parent may be gone
                logger.warning(f"AgentControl: completion notify to {parent_id} failed: {exc}")

        task = asyncio.create_task(_watch())
        self._watchers.append(task)
        return task

    def start_ttl_watchdog(self, child_id: str, timeout_seconds: float) -> asyncio.Task[None]:
        """Interrupt *child_id* once it has lived *timeout_seconds* wall-clock.

        The MANAGED wall-clock deadline: a total time-to-live, not a per-turn
        cap. Sleeps out the budget, then — if the child is still live and not
        already final — calls the existing :meth:`interrupt` (→ INTERRUPTED). The
        completion watcher (already running for a watched child) folds the usual
        queue-only parent notice, and ``release_child`` recycles it, so this adds
        only the alarm. A child that finishes early is dropped from ``_runtimes``,
        so the post-sleep lookup no-ops. Like the completion watcher it holds a
        :class:`weakref` to ``self`` so a dropped plane lets the watchdog bail.
        """
        weak_self = weakref.ref(self)

        async def _watchdog() -> None:
            await asyncio.sleep(timeout_seconds)
            ctrl = weak_self()
            if ctrl is None:
                return
            child = ctrl._runtimes.get(child_id)
            if child is None or is_final(child.status):
                return  # finished/evicted before the deadline → nothing to do
            logger.warning(f"AgentControl: {child_id} exceeded its {timeout_seconds}s TTL; interrupting.")
            try:
                await ctrl.interrupt(child_id)
            except Exception as exc:  # noqa: BLE001 — interrupt is best-effort
                logger.warning(f"AgentControl: TTL interrupt of {child_id} failed: {exc}")

        task = asyncio.create_task(_watchdog())
        self._watchers.append(task)
        return task

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def start(self) -> None:
        self._start_telemetry()
        self._scheduler.start()
        # Event-driven fulfilment for the fleet-idle case (persistent mode only;
        # bounded ``run(k)`` fulfils inline at each pump round instead).
        if self._pending_waker_task is None or self._pending_waker_task.done():
            self._pending_waker_task = asyncio.create_task(self._pending_waker_loop())

    async def stop(self) -> None:
        for task in self._watchers:
            if not task.done():
                task.cancel()
        self._watchers.clear()
        if self._pending_waker_task is not None and not self._pending_waker_task.done():
            self._pending_waker_task.cancel()
            try:
                await self._pending_waker_task
            except asyncio.CancelledError:
                pass
            except Exception as exc:  # noqa: BLE001 — waker crashed during stop
                logger.debug(f"AgentControl: pending-waker task raised during stop: {exc}")
        self._pending_waker_task = None
        try:
            await self._scheduler.stop()
        finally:
            await self._telemetry.aclose()

    async def run_ready_turns(self, max_turns: int = 1) -> int:
        self._start_telemetry()
        return await self._scheduler.run_ready_turns(max_turns)

    def _start_telemetry(self) -> None:
        if self._telemetry.state is TelemetryState.NEW:
            self._telemetry.start()
            for runtime in self._runtimes.values():
                self._telemetry.emit_sync(
                    AgentLifecycleEvent(
                        session_id=runtime.session_id,
                        phase="added",
                        detail=type(runtime.role).__name__,
                    )
                )
            return
        if self._telemetry.state not in {
            TelemetryState.RUNNING,
            TelemetryState.DEGRADED,
        }:
            raise RuntimeError("agent control telemetry is closed")

    def quiescent(self) -> bool:
        """True when the fleet has no running, woken, or *parked* trigger work.

        Parked trigger-turn deliveries are outstanding work too: a caller pumping
        ``while not quiescent(): await run(k)`` must keep going until they are
        fulfilled (a slot frees → flush rehydrates + delivers), so quiescence
        accounts for them alongside the scheduler's own readiness.
        """
        if self._pending.has_trigger_pending():
            return False
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
