#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""AgentControl — the session-scoped multi-agent control plane.

Port of the consumer-facing surface of ``codex-rs/core/src/agent/control.rs``
(the codex-specific spawn/rollout/shell-snapshot machinery is intentionally
omitted — this round is "plane infrastructure only"). ``AgentControl`` ties the
five primitives together and owns the single source of truth for liveness:

  * :class:`AgentRegistry`  — path/nickname identity index,
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
import hashlib
import re
import uuid
import weakref
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import Callable, Dict, Optional, TypeVar

from mote.contracts.agent import (
    AgentConstructionRequest,
    ContextPolicy,
    CostAttributionPort,
    Lifecycle,
    RunnableAgent,
    SpawnContext,
    SpawnPlan,
)
from mote.contracts.agent.budget import (
    AgentBudgetDisposition,
    AgentBudgetPolicy,
    AgentBudgetRequest,
    AgentBudgetReservationReceipt,
)
from mote.contracts.agent.cancellation import (
    AgentCancellationCommand,
    AgentCancellationDisposition,
    AgentCancellationReceipt,
    SubtreeCancellationReceipt,
)
from mote.contracts.agent.capacity import (
    CapacityReservationDisposition,
    CapacitySettlementDisposition,
    LogicalCapacityLimit,
    LogicalCapacityScope,
    LogicalCapacityScopeKind,
)
from mote.contracts.agent.delivery import AgentDeliveryState
from mote.contracts.agent.errors import AgentLimitReached, AgentNotFound, AgentNotKnown
from mote.contracts.agent.lineage import LineageRecord, SpawnAdvanceDisposition, SpawnLifecycle, SpawnRequest
from mote.contracts.agent.policy import SpawnIntent
from mote.contracts.agent.runtime_identity import AgentId, CancellationEpoch, IncarnationGeneration, LineageRevision
from mote.contracts.conversation import Message, UserMessage, load_message
from mote.contracts.events.agent import AgentLifecycleEvent
from mote.contracts.ports.agent.budget import AgentBudgetPort
from mote.contracts.ports.agent.control import ChildReleaseDisposition, ChildReleaseReceipt
from mote.contracts.ports.agent.delivery import (
    AgentDeliveryCommand,
    AgentDeliveryCommandDisposition,
    AgentDeliveryCommandReceipt,
)
from mote.contracts.ports.agent.spawn_policy import SpawnPolicyExtensionSpec
from mote.contracts.ports.agent.team_roster import TeamRosterMember
from mote.contracts.ports.events.telemetry import TelemetryIdentity, TelemetryOverflow, TelemetrySubscriptionSpec
from mote.contracts.ports.runtime.lease import LeaseCoordinator, LeaseEpoch
from mote.contracts.ports.workflow.delivery import WorkflowAgentDeliveryCompositionPort
from mote.contracts.ports.workflow.governance import WorkflowGovernanceCompositionPort
from mote.contracts.runtime.errors import LeaseFencedError
from mote.contracts.workflow.admission import (
    ClaimWorkflowCreateAdmission,
    ReserveWorkflowCreateAdmission,
    SettleWorkflowCreateAdmission,
    WorkflowCreateAdmission,
    WorkflowCreateAdmissionDisposition,
    WorkflowCreateAdmissionLifecycle,
    WorkflowCreateAdmissionReceipt,
)
from mote.contracts.workflow.authority import (
    WorkflowCallerAuthorizationDisposition,
    WorkflowCallerAuthorizationReceipt,
    WorkflowCallerContext,
    WorkflowCreateAdmissionId,
)
from mote.contracts.workflow.governance import WorkflowGovernanceCancelRequest, WorkflowGovernanceSnapshotVerification
from mote.orchestration.agents.cancellation import SubtreeCancellationCoordinator
from mote.orchestration.agents.capacity import LogicalCapacityProjection
from mote.orchestration.agents.control_context import bind_workflow_caller_control
from mote.orchestration.agents.execution.turn_scheduler import EventDrivenScheduler
from mote.orchestration.agents.identity.path import AgentPath
from mote.orchestration.agents.identity.registry import AgentMetadata, AgentRegistry, next_agent_spawn_depth
from mote.orchestration.agents.ingress.reconcile import AgentIngressReconciler, AgentIngressReconcileResult
from mote.orchestration.agents.lifecycle.admission import build_spawn_admission_policy
from mote.orchestration.agents.lifecycle.handle import ChildAgentHandle
from mote.orchestration.agents.lifecycle.runtime import AgentRuntime, AgentStatus, is_final
from mote.orchestration.agents.lifecycle.spawn import SpawnPhase, SpawnTransaction
from mote.orchestration.agents.lineage.store import AgentLineageStore
from mote.orchestration.agents.lineage.workflow_authority import AgentLineageWorkflowCallerAuthorizer
from mote.orchestration.agents.lineage.workflow_governance import AgentLineageWorkflowGovernanceVerifier
from mote.orchestration.agents.messaging.durable import AgentDeliveryStore
from mote.orchestration.agents.messaging.mailbox import DeliveryMode, InterAgentCommunication
from mote.orchestration.agents.messaging.pending import PendingDelivery, PendingDeliveryQueue
from mote.orchestration.agents.messaging.routing import CommGraph, CommKind
from mote.orchestration.agents.residency.lifecycle import ResidentLifecyclePhase, ResidentTransitionClaim
from mote.orchestration.agents.residency.manager import Residency, ResidencySlot
from mote.orchestration.agents.residency.model import ResidencyIdentity
from mote.orchestration.agents.residency.store import ResidencyStore
from mote.orchestration.agents.turn_queue.limiter import AgentExecutionLimiter
from mote.orchestration.agents.turn_queue.scheduling import RootTurnWeight, TurnSchedulingConfig
from mote.orchestration.agents.turn_queue.store import DurableTurnQueueStore
from mote.runtime.agent.base import BaseRole
from mote.runtime.agent.control import set_control
from mote.runtime.agent.incarnation import AgentIncarnationError, AgentIncarnationFactory
from mote.runtime.clock import SystemClock
from mote.runtime.control.leases import LeaseHandle
from mote.runtime.events.log_subscriber import LogSubscriber
from mote.runtime.events.telemetry import AllTelemetryBinding, TelemetryManifest, TelemetryRuntime, TelemetryState
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


def _turn_queue_path(lineage_path: Path, queue_id: str) -> Path:
    identity_digest = hashlib.sha256(queue_id.encode("utf-8")).hexdigest()
    return lineage_path.with_name(f"agent-turn-queue-{identity_digest}.json")


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
        max_logical_agents: Optional[int] = None,
        max_resident_incarnations: Optional[int] = None,
        max_concurrent_turns: Optional[int] = None,
        max_depth: Optional[int] = None,
        max_cost_usd: Optional[float] = None,
        max_total_tokens: Optional[int] = None,
        spawn_policy_extensions: tuple[SpawnPolicyExtensionSpec, ...] = (),
        incarnation_factory: AgentIncarnationFactory | None = None,
        residency_lease_coordinator: LeaseCoordinator,
        lineage_path: Path,
        turn_queue_capacity: int,
        root_turn_weights: tuple[tuple[str, int], ...] = (),
        budget: AgentBudgetPort | None = None,
        budget_policy: AgentBudgetPolicy | None = None,
        child_token_reservation: int | None = None,
        child_cost_micro_usd_reservation: int | None = None,
        workflow_governance: WorkflowGovernanceCompositionPort | None = None,
        workflow_delivery: WorkflowAgentDeliveryCompositionPort | None = None,
        watch_interval: float = 0.01,
    ):
        self.session_id = session_id
        self._runtimes: Dict[str, AgentRuntime] = {}
        self._registry = AgentRegistry()
        self._logical_capacity = LogicalCapacityProjection(lineage_path.with_name("agent-logical-capacity.json"))
        self._logical_reservations: dict[str, str] = {}
        self._limiter = AgentExecutionLimiter()
        self._max_logical_agents = max_logical_agents
        # Admission limits are immutable composition inputs read by the sealed
        # SpawnAdmissionPolicy for every resolved spawn intent.
        self._max_depth = max_depth
        self._max_cost_usd = max_cost_usd
        self._max_total_tokens = max_total_tokens
        budget_values = (
            budget,
            budget_policy,
            child_token_reservation,
            child_cost_micro_usd_reservation,
        )
        if any(value is None for value in budget_values) != all(value is None for value in budget_values):
            raise ValueError("Agent budget governance dependencies must be complete")
        self._budget = budget
        self._budget_policy = budget_policy
        self._child_token_reservation = child_token_reservation
        self._child_cost_micro_usd_reservation = child_cost_micro_usd_reservation
        self._budget_reservations: dict[str, AgentBudgetReservationReceipt] = {}
        self._spawn_policy = build_spawn_admission_policy(spawn_policy_extensions)
        if max_concurrent_turns is not None:
            self._limiter.initialize(max_concurrent_turns)
        # The orchestration plane owns a process-local, loss-tolerant telemetry
        # runtime because it lives outside every per-Role observation scope.
        self._telemetry = TelemetryRuntime(
            TelemetryManifest(
                (
                    AllTelemetryBinding(
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
        turn_clock = SystemClock()
        turn_queue_subject = f"agent-turn-queue:{self.session_id or 'application'}"
        turn_queue_owner = f"agent-turn-scheduler:{uuid.uuid4().hex}"
        turn_queue_lease = residency_lease_coordinator.acquire(turn_queue_subject, turn_queue_owner, 30.0)
        turn_queue_store = DurableTurnQueueStore(
            _turn_queue_path(lineage_path, turn_queue_subject),
            queue_id=turn_queue_subject,
            capacity=turn_queue_capacity,
            lease_coordinator=residency_lease_coordinator,
        )
        self._turn_queue_lease = turn_queue_lease
        self._turn_queue_store = turn_queue_store
        self._scheduler = EventDrivenScheduler(
            limiter=self._limiter,
            control_binder=self._turn_control_binding,
            pending_flush=self._flush_pending_deliveries,
            delivery_ack=self._ack_deliveries,
            delivery_bind=self._bind_deliveries,
            durable_store=turn_queue_store,
            durable_lease=turn_queue_lease,
            scheduling_config=TurnSchedulingConfig(
                1,
                tuple(RootTurnWeight(root_id, weight) for root_id, weight in root_turn_weights),
            ),
            now=turn_clock.now,
            process_instance_id=turn_queue_owner,
            root_owner_id=self.session_id or "application",
        )
        self._residency = Residency(
            self._runtimes.get,
            store=self._store,
            remove_runtime=self._remove_runtime,
            telemetry=self._telemetry,
            materialization_authority=self._residency_materialization_authority,
        )
        self._max_resident_incarnations = max_resident_incarnations
        self._incarnation_factory = incarnation_factory or AgentIncarnationFactory()
        self._residency_lease_coordinator = residency_lease_coordinator
        self._residency_owner_id = f"agent-control:{uuid.uuid4().hex}"
        self._lineage = AgentLineageStore(lineage_path, lease_coordinator=residency_lease_coordinator)
        self._lineage_lease = residency_lease_coordinator.acquire(
            f"agent-lineage:{self.session_id or 'application'}",
            self._residency_owner_id,
            30.0,
        )
        self._delivery_subject = f"agent-delivery:{self.session_id or 'application'}"
        self._delivery_lease = residency_lease_coordinator.acquire(
            self._delivery_subject, self._residency_owner_id, 30.0
        )
        self._turn_queue_lease_handle = LeaseHandle(
            residency_lease_coordinator,
            subject=turn_queue_subject,
            owner_id=turn_queue_owner,
        )
        self._lineage_lease_handle = LeaseHandle(
            residency_lease_coordinator,
            subject=self._lineage_lease.subject,
            owner_id=self._residency_owner_id,
        )
        self._delivery_lease_handle = LeaseHandle(
            residency_lease_coordinator,
            subject=self._delivery_subject,
            owner_id=self._residency_owner_id,
        )
        self._lease_heartbeats_started = False
        self._delivery_store = AgentDeliveryStore(
            lineage_path.with_name("agent-deliveries.json"),
            leases=residency_lease_coordinator,
            subject=self._delivery_subject,
        )
        self._lineage_root_id = self.session_id or "root"
        self._lineage.register_root(self._lineage_root_id, "mote.agent.root/runtime", lease=self._lineage_lease)
        self._workflow_governance = workflow_governance
        self._workflow_governance_registered = workflow_governance is not None
        if workflow_governance is not None:
            workflow_governance.register_agent_governance(AgentId(self._lineage_root_id), self, self)
        self._workflow_delivery = workflow_delivery
        self._workflow_delivery_registered = workflow_delivery is not None
        if workflow_delivery is not None:
            workflow_delivery.register_agent_delivery(AgentId(self._lineage_root_id), self)
        if self.session_id is not None:
            self._registry.register_root_agent(self.session_id)
            for record in self._lineage.records():
                if (
                    record.logical_agent_id is None
                    or record.logical_agent_id == self.session_id
                    or record.tombstoned
                    or record.lifecycle is not SpawnLifecycle.ACTIVE
                ):
                    continue
                self._registry.register_spawned_agent(
                    AgentMetadata(
                        agent_id=record.logical_agent_id,
                        agent_path=AgentPath.from_string(record.request.agent_path),
                        agent_nickname=record.request.nickname,
                        agent_role=record.request.definition_id,
                    )
                )
                if self._logical_capacity.reservation(record.request.capacity_reservation_id) is None:
                    raise RuntimeError("active lineage has no committed logical capacity reservation")
                self._logical_reservations[record.logical_agent_id] = record.request.capacity_reservation_id
                budget_reservation_ids = record.request.budget_reservation_ids
                if self._budget is not None:
                    if not budget_reservation_ids:
                        raise RuntimeError("active child lineage has no committed budget reservations")
                    budget_reservations = self._budget.reservations_by_id(budget_reservation_ids)
                    if (
                        tuple(reservation.reservation_id for reservation in budget_reservations)
                        != budget_reservation_ids
                    ):
                        raise RuntimeError("active child lineage budget reservation identity mismatch")
                    self._budget_reservations[record.logical_agent_id] = AgentBudgetReservationReceipt(
                        record.request.request_id,
                        AgentBudgetDisposition.RESERVED,
                        budget_reservations,
                    )
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
        for record in self._delivery_store.pending():
            message = load_message(record.message_payload)
            if message is None:
                raise ValueError("durable delivery message is invalid")
            self._pending.park(
                record.target_agent_id,
                PendingDelivery(
                    delivery_id=record.delivery_id,
                    target_generation=record.target_generation,
                    message=message,
                    mode=DeliveryMode(record.mode),
                ),
            )
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

    def workflow_caller_context(self, agent_id: AgentId) -> WorkflowCallerContext:
        record = self._lineage.record_for_agent(str(agent_id))
        if record is None:
            raise AgentNotFound(str(agent_id))
        return WorkflowCallerContext(
            agent_id,
            AgentId(record.request.root_agent_id),
            IncarnationGeneration(record.incarnation_generation),
            LineageRevision(record.revision),
            CancellationEpoch(record.cancellation_epoch),
            record.owner_fencing_token,
        )

    def authorize_workflow_caller(self, caller: WorkflowCallerContext) -> WorkflowCallerAuthorizationReceipt:
        return AgentLineageWorkflowCallerAuthorizer(self._lineage).authorize_workflow_caller(caller)

    def verify(self, request: WorkflowGovernanceCancelRequest) -> WorkflowGovernanceSnapshotVerification:
        return AgentLineageWorkflowGovernanceVerifier(self._lineage).verify(request)

    def get_workflow_create_admission(self, admission_id: WorkflowCreateAdmissionId) -> WorkflowCreateAdmission | None:
        return self._lineage.get_workflow_create_admission(admission_id)

    def reserved_workflow_create_admissions(
        self,
    ) -> tuple[WorkflowCreateAdmission, ...]:
        return tuple(
            item
            for item in self._lineage.workflow_create_admissions()
            if item.lifecycle is WorkflowCreateAdmissionLifecycle.RESERVED
        )

    def claim_workflow_create_admission(self, command: ClaimWorkflowCreateAdmission) -> WorkflowCreateAdmissionReceipt:
        return self._lineage.claim_workflow_create(
            command.admission_id,
            expected_revision=command.expected_revision,
            ownership=command.ownership,
            lease=self._lineage_lease,
        )

    def reserve_workflow_create_admission(
        self, command: ReserveWorkflowCreateAdmission
    ) -> WorkflowCreateAdmissionReceipt:
        authorization = self.authorize_workflow_caller(command.caller)
        if (
            authorization.disposition is not WorkflowCallerAuthorizationDisposition.AUTHORIZED
            or authorization.lineage_revision != command.caller.lineage_revision
            or command.ownership.request.operation_id != command.admission_id
        ):
            return WorkflowCreateAdmissionReceipt(WorkflowCreateAdmissionDisposition.FENCE_LOST, None)
        return self._lineage.reserve_workflow_create(
            admission_id=command.admission_id,
            create_request_id=command.create_request_id,
            reference=command.reference,
            logical_agent_id=str(command.caller.logical_agent_id),
            expected_lineage_revision=int(command.caller.lineage_revision),
            cancellation_epoch=int(command.cancellation_epoch),
            ownership=command.ownership,
            lease=self._lineage_lease,
        )

    def settle_workflow_create_admission(
        self, command: SettleWorkflowCreateAdmission
    ) -> WorkflowCreateAdmissionReceipt:
        if command.ownership.request.operation_id != command.admission_id:
            return WorkflowCreateAdmissionReceipt(WorkflowCreateAdmissionDisposition.FENCE_LOST, None)
        return self._lineage.settle_workflow_create(
            command.admission_id,
            command.lifecycle,
            expected_revision=command.expected_revision,
            ownership=command.ownership,
            lease=self._lineage_lease,
        )

    @contextmanager
    def _turn_control_binding(self):
        with ExitStack() as stack:
            stack.enter_context(set_control(self))
            stack.enter_context(bind_workflow_caller_control(self))
            yield self

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
        incarnation_generation = self._register_incarnation(runtime.role, session_id)
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
        self._residency.register_active(session_id, incarnation_generation)
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

    def _register_incarnation(self, role: object, session_id: str) -> int:
        """Retain a construction-only blueprint across in-process eviction."""

        if isinstance(role, BaseRole):
            self._incarnation_factory.register(
                session_id,
                role.incarnation_blueprint(),
            )
            return self._incarnation_factory.generation(session_id)
        return 1

    def _residency_lease(self, agent_id: str) -> LeaseEpoch:
        return self._residency_lease_coordinator.acquire(f"agent-residency:{agent_id}", self._residency_owner_id, 30.0)

    def _residency_identity(self, agent_id: str) -> ResidencyIdentity:
        metadata = self._registry.agent_metadata_for_id(agent_id)
        if metadata is None or metadata.agent_path is None:
            raise RuntimeError(f"Agent {agent_id!r} has no canonical lineage identity")
        root_id = self._registry.agent_id_for_path(AgentPath.root())
        if root_id is None:
            raise RuntimeError("Agent tree has no canonical root identity")
        parent_path = metadata.agent_path.parent()
        parent_id = None if parent_path is None else self._registry.agent_id_for_path(parent_path)
        blueprint = self._incarnation_factory.factory(agent_id)
        return ResidencyIdentity(
            logical_agent_id=agent_id,
            root_agent_id=root_id,
            parent_agent_id=parent_id,
            agent_path=metadata.agent_path.as_str(),
            nickname=metadata.agent_nickname,
            definition_id=blueprint.definition_id,
            config_digest=blueprint.config_digest,
            incarnation_generation=self._incarnation_factory.generation(agent_id),
        )

    def _residency_materialization_authority(self, runtime: AgentRuntime) -> tuple[ResidencyIdentity, LeaseEpoch]:
        return self._residency_identity(runtime.session_id), self._residency_lease(runtime.session_id)

    # ------------------------------------------------------------------
    # Cost mirror tree
    # ------------------------------------------------------------------
    @staticmethod
    def _role_cost_tracker(
        role: RunnableAgent[OutputT],
    ) -> CostTracker:
        """Read a role's cost bucket through its public attribution seam."""
        attribution = role.spawn_cost_attribution()
        if not isinstance(attribution, CostTracker):
            raise TypeError("Runtime Agent cost attribution must be backed by CostTracker")
        return attribution

    def _register_cost_root(self, runtime: AgentRuntime, agent_id: str) -> None:
        """Seed the cost tree root from the root agent's own tracker."""
        tracker = self._role_cost_tracker(runtime.role)
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
        lineage_record = None
        existing_lineage = self._lineage.record_for_request(spec.request_id)
        if (
            existing_lineage is not None
            and existing_lineage.lifecycle is SpawnLifecycle.ACTIVE
            and existing_lineage.logical_agent_id is not None
        ):
            existing_runtime = self._runtimes.get(existing_lineage.logical_agent_id)
            if existing_runtime is None:
                raise RuntimeError("active durable spawn is not resident; reconciliation is required")
            return ChildAgentHandle(
                existing_runtime,
                control=self,
                agent_id=existing_lineage.logical_agent_id,
                agent_path=AgentPath.from_string(existing_lineage.request.agent_path),
            )
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
        logical_receipt = self._reserve_logical_capacity(parent_path)
        transaction.own(lambda: self._rollback_logical_capacity(logical_receipt.reservation_id))
        budget_receipt = await self._reserve_agent_budget(
            spec.request_id,
            parent_path=parent_path,
            child_depth=child_depth,
        )
        if budget_receipt is not None:
            transaction.own(lambda: self._settle_agent_budget(budget_receipt, actual_tokens=0, actual_cost_micro_usd=0))
        slot = await self._residency.reserve_slot(self._max_resident_incarnations, protected_session_id=spec.parent_id)
        transaction.advance(SpawnPhase.RESIDENCY_RESERVED, lambda: self._rollback_residency_slot(slot))
        try:
            # Identity bookkeeping only (no cap here — identities persist across
            # eviction; the live ceiling is the residency slot above).
            reservation = self._registry.reserve_spawn_identity()
            transaction.advance(SpawnPhase.IDENTITY_RESERVED, reservation.rollback)
            base = _sanitize_segment(spec.nickname or spec.agent_role or "agent")
            segment = f"{base}_{spec.request_id[:8]}"
            nickname = reservation.reserve_agent_nickname_with_preference([base], preferred=segment)
            child_path = parent_path.join(segment)
            reservation.reserve_agent_path(child_path)

            lineage_request = SpawnRequest(
                request_id=spec.request_id,
                root_agent_id=self._lineage_root_id,
                parent_agent_id=spec.parent_id or self._lineage_root_id,
                agent_path=child_path.as_str(),
                nickname=nickname,
                definition_id=spec.definition.version,
                capacity_reservation_id=logical_receipt.reservation_id,
                budget_reservation_ids=(
                    ()
                    if budget_receipt is None
                    else tuple(reservation.reservation_id for reservation in budget_receipt.reservations)
                ),
            )
            requested = self._lineage.request_spawn(
                lineage_request,
                capacity=logical_receipt,
                budget=budget_receipt,
                lease=self._lineage_lease,
            )
            if requested.disposition.value not in {"applied", "idempotent"} or requested.record is None:
                raise RuntimeError(f"durable spawn request rejected: {requested.disposition.value}")
            lineage_record = requested.record
            if lineage_record.lifecycle is SpawnLifecycle.REQUESTED:
                lineage_record = self._require_lineage_advance(lineage_record, SpawnLifecycle.ADMITTED)
            if lineage_record.lifecycle is SpawnLifecycle.ADMITTED:
                lineage_record = self._require_lineage_advance(lineage_record, SpawnLifecycle.LINEAGE_COMMITTED)
            if lineage_record.lifecycle is SpawnLifecycle.LINEAGE_COMMITTED:
                lineage_record = self._require_lineage_advance(
                    lineage_record,
                    SpawnLifecycle.PLACEMENT_PENDING,
                    placement=self._residency_owner_id,
                )
            if lineage_record.logical_agent_id is None:
                raise RuntimeError("durable lineage did not commit a logical Agent identity")

            spawn_ctx = self._build_spawn_context(spec, child_path)
            request = AgentConstructionRequest(
                logical_agent_id=lineage_record.logical_agent_id,
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
            transaction.advance(SpawnPhase.PROVISIONED)

            runtime = AgentRuntime(role, agent_path=child_path)
            agent_id = runtime.session_id
            if agent_id != lineage_record.logical_agent_id:
                raise RuntimeError("constructed Agent identity differs from durable lineage")
            incarnation_generation = self._register_incarnation(role, agent_id)
            lineage_record = self._require_lineage_advance(
                lineage_record,
                SpawnLifecycle.INCARNATION_STARTED,
                placement=self._residency_owner_id,
                incarnation_generation=incarnation_generation,
            )
            transaction.own(lambda: self._incarnation_factory.unregister(agent_id))
            self._add_cost_node(role, spec.parent_id, agent_id, child_path)
            transaction.own(lambda: self._remove_cost_node(agent_id))

            # Install all supervision while the child remains absent from the
            # identity registry. create_task cannot run until this synchronous
            # commit section yields, so no watcher observes partial state.
            self._runtimes[agent_id] = runtime
            transaction.own(lambda: self._remove_runtime(agent_id))
            self._residency.register_active(
                agent_id,
                incarnation_generation,
                resident=False,
            )
            transaction.own(lambda: self._residency.forget_uncommitted(agent_id))
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
            self._logical_reservations[agent_id] = logical_receipt.reservation_id
            if budget_receipt is not None:
                self._budget_reservations[agent_id] = budget_receipt
                transaction.own(lambda: self._discard_budget_reservation(agent_id))
            transaction.own(lambda: self._discard_logical_reservation(agent_id))
            transaction.own(lambda: self._registry.release_spawned_agent(agent_id))
            if spec.lifecycle is Lifecycle.MANAGED:
                slot.commit(agent_id, incarnation_generation)
                transaction.own(lambda: self._residency.remove(agent_id))
            lineage_record = self._require_lineage_advance(lineage_record, SpawnLifecycle.ACTIVE)
            transaction.commit()
        except BaseException:
            if lineage_record is not None and lineage_record.lifecycle not in {
                SpawnLifecycle.ACTIVE,
                SpawnLifecycle.REJECTED,
                SpawnLifecycle.ABORTED,
                SpawnLifecycle.TERMINAL,
            }:
                aborted = self._lineage.advance(
                    lineage_record.request.request_id,
                    SpawnLifecycle.ABORTED,
                    expected_revision=lineage_record.revision,
                    lease=self._lineage_lease,
                )
                if aborted.record is not None:
                    lineage_record = aborted.record
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

    async def release_child(self, agent_id: str) -> ChildReleaseReceipt:
        """Settle one logical child before releasing resident capacity or indices."""
        snapshot = self._residency.lifecycle_snapshot(agent_id)
        if snapshot is None:
            if self._store.has(agent_id):
                try:
                    generation = self._incarnation_factory.generation(agent_id)
                except AgentIncarnationError:
                    return ChildReleaseReceipt(
                        agent_id,
                        ChildReleaseDisposition.OWNER_LOST,
                        0,
                        "trusted incarnation generation unavailable",
                    )
                self._residency.ensure_evicted(agent_id, generation)
                snapshot = self._residency.lifecycle_snapshot(agent_id)
        if snapshot is None:
            return ChildReleaseReceipt(agent_id, ChildReleaseDisposition.ALREADY_TERMINAL, 0)
        if snapshot.phase in {
            ResidentLifecyclePhase.TERMINAL,
            ResidentLifecyclePhase.TOMBSTONED,
            ResidentLifecyclePhase.PURGED,
        }:
            return ChildReleaseReceipt(
                agent_id,
                ChildReleaseDisposition.ALREADY_TERMINAL,
                snapshot.revision,
            )
        if snapshot.phase is ResidentLifecyclePhase.TERMINATING:
            terminating_snapshot = snapshot
        else:
            terminating = self._residency.begin_termination(
                agent_id,
                expected_generation=snapshot.incarnation_generation,
                expected_revision=snapshot.revision,
            )
            if terminating.disposition.value != "applied":
                return ChildReleaseReceipt(
                    agent_id,
                    ChildReleaseDisposition.OWNER_LOST,
                    terminating.snapshot.revision,
                )
            terminating_snapshot = terminating.snapshot
        runtime = self._runtimes.get(agent_id)
        try:
            if runtime is not None:
                self._scheduler.remove_runtime(agent_id)
                await runtime.shutdown()
                await runtime.role.cleanup()
        except Exception as exc:  # noqa: BLE001 -- keep TERMINATING and capacity pinned
            return ChildReleaseReceipt(
                agent_id,
                ChildReleaseDisposition.CLEANUP_FAILED,
                terminating_snapshot.revision,
                type(exc).__name__,
            )
        budget_receipt = self._budget_reservations.get(agent_id)
        if budget_receipt is not None:
            usage = self._cost_nodes.get(agent_id)
            try:
                await self._settle_agent_budget(
                    budget_receipt,
                    actual_tokens=(0 if usage is None else usage.tracker.attributed_total_tokens()),
                    actual_cost_micro_usd=(
                        0
                        if usage is None
                        else max(
                            0,
                            round(usage.tracker.attributed_cost_usd() * 1_000_000),
                        )
                    ),
                )
            except Exception as exc:  # noqa: BLE001 -- retain TERMINATING for retry
                return ChildReleaseReceipt(
                    agent_id,
                    ChildReleaseDisposition.CLEANUP_FAILED,
                    terminating_snapshot.revision,
                    f"budget settlement failed: {type(exc).__name__}",
                )
            self._budget_reservations.pop(agent_id, None)
        reservation_id = self._logical_reservations.get(agent_id)
        if reservation_id is not None:
            settlement = self._settle_logical_reservation(reservation_id)
            if settlement.disposition not in {
                CapacitySettlementDisposition.SETTLED,
                CapacitySettlementDisposition.ALREADY_SETTLED,
            }:
                return ChildReleaseReceipt(
                    agent_id,
                    ChildReleaseDisposition.OWNER_LOST,
                    terminating_snapshot.revision,
                    settlement.disposition.value,
                )
            self._logical_reservations.pop(agent_id, None)
        terminal = self._residency.complete_termination(
            agent_id,
            expected_generation=terminating_snapshot.incarnation_generation,
            expected_revision=terminating_snapshot.revision,
        )
        if terminal.disposition.value != "applied":
            return ChildReleaseReceipt(
                agent_id,
                ChildReleaseDisposition.OWNER_LOST,
                terminal.snapshot.revision,
            )
        lineage = self._lineage.record_for_agent(agent_id)
        if lineage is not None and lineage.lifecycle is not SpawnLifecycle.TERMINAL:
            lineage_terminal = self._lineage.advance(
                lineage.request.request_id,
                SpawnLifecycle.TERMINAL,
                expected_revision=lineage.revision,
                lease=self._lineage_lease,
            )
            if lineage_terminal.disposition not in {
                SpawnAdvanceDisposition.APPLIED,
                SpawnAdvanceDisposition.IDEMPOTENT,
            }:
                return ChildReleaseReceipt(
                    agent_id,
                    ChildReleaseDisposition.OWNER_LOST,
                    terminal.snapshot.revision,
                    "durable lineage terminal settlement failed",
                )
        self._remove_runtime(agent_id)
        self._residency.remove(agent_id)
        self._registry.release_spawned_agent(agent_id)
        self._remove_cost_node(agent_id)
        self._comm_graph.remove(agent_id)
        self._delivery_store.dead_letter_target(agent_id, "logical_agent_terminal", lease=self._delivery_lease)
        self._pending.drop(agent_id)
        self._incarnation_factory.unregister(agent_id)
        tombstone = self._residency.tombstone(
            agent_id,
            expected_generation=terminal.snapshot.incarnation_generation,
            expected_revision=terminal.snapshot.revision,
        )
        if tombstone.disposition.value != "applied":
            return ChildReleaseReceipt(
                agent_id,
                ChildReleaseDisposition.OWNER_LOST,
                tombstone.snapshot.revision,
            )
        return ChildReleaseReceipt(agent_id, ChildReleaseDisposition.SETTLED, tombstone.snapshot.revision)

    async def cancel_agent_scope(self, command: AgentCancellationCommand) -> AgentCancellationReceipt:
        if not self._lineage.cancellation_target_is_current(
            root_agent_id=command.root_agent_id,
            subtree_agent_id=command.subtree_agent_id,
            target_agent_id=command.target_agent_id,
            lineage_revision=command.lineage_revision,
            cancellation_epoch=command.cancellation_epoch,
        ):
            return AgentCancellationReceipt(
                command.target_agent_id,
                command.cancellation_epoch,
                AgentCancellationDisposition.OWNER_LOST,
                "lineage cancellation epoch is stale",
            )
        self._scheduler.cancel_agent_turns(command.target_agent_id)
        released = await self.release_child(command.target_agent_id)
        disposition = {
            ChildReleaseDisposition.SETTLED: AgentCancellationDisposition.SETTLED,
            ChildReleaseDisposition.ALREADY_TERMINAL: AgentCancellationDisposition.ALREADY_TERMINAL,
            ChildReleaseDisposition.CLEANUP_FAILED: AgentCancellationDisposition.TIMEOUT,
            ChildReleaseDisposition.OWNER_LOST: AgentCancellationDisposition.OWNER_LOST,
        }[released.disposition]
        return AgentCancellationReceipt(
            command.target_agent_id,
            command.cancellation_epoch,
            disposition,
            released.detail,
        )

    async def cancel_subtree(
        self,
        subtree_agent_id: str,
        *,
        timeout_seconds: float = 30.0,
        cancellation_epoch: int | None = None,
    ) -> SubtreeCancellationReceipt:
        """Issue one fenced epoch command to each Agent in a stable subtree."""
        return await SubtreeCancellationCoordinator(self._lineage, self, self._workflow_governance).cancel(
            subtree_agent_id,
            lease=self._lineage_lease,
            timeout_seconds=timeout_seconds,
            cancellation_epoch=cancellation_epoch,
        )

    def _reserve_logical_capacity(self, parent_path: AgentPath):
        application = self.session_id or "agent-control"
        path = parent_path.as_str()
        segments = path.strip("/").split("/")
        root = segments[1] if len(segments) > 1 else application
        scopes = (
            LogicalCapacityScope(LogicalCapacityScopeKind.APPLICATION, application),
            LogicalCapacityScope(LogicalCapacityScopeKind.ROOT, root),
            LogicalCapacityScope(LogicalCapacityScopeKind.SUBTREE, path),
            LogicalCapacityScope(LogicalCapacityScopeKind.PARENT, path),
        )
        limits = (
            ()
            if self._max_logical_agents is None
            else tuple(LogicalCapacityLimit(scope, self._max_logical_agents) for scope in scopes)
        )
        while True:
            receipt = self._logical_capacity.reserve(limits, expected_revision=self._logical_capacity.revision)
            if receipt.disposition is CapacityReservationDisposition.REVISION_CONFLICT:
                continue
            if receipt.disposition is CapacityReservationDisposition.REJECTED_CAPACITY:
                raise AgentLimitReached(message="logical Agent capacity is exhausted")
            return receipt

    async def _reserve_agent_budget(
        self,
        request_id: str,
        *,
        parent_path: AgentPath,
        child_depth: int,
    ) -> AgentBudgetReservationReceipt | None:
        if self._budget is None:
            return None
        assert self._budget_policy is not None
        assert self._child_token_reservation is not None
        assert self._child_cost_micro_usd_reservation is not None
        receipt = await self._budget.reserve(
            AgentBudgetRequest(
                request_id=request_id,
                root_id=self._lineage_root_id,
                subtree_id=parent_path.as_str(),
                agent_id=uuid.uuid5(uuid.NAMESPACE_URL, f"mote-agent:{request_id}").hex,
                requested_tokens=self._child_token_reservation,
                requested_cost_micro_usd=self._child_cost_micro_usd_reservation,
                child_depth=child_depth,
                capabilities=frozenset({"delegate"}),
            ),
            self._budget_policy,
        )
        if receipt.disposition is not AgentBudgetDisposition.RESERVED:
            raise AgentLimitReached(message=f"Agent budget admission failed: {receipt.disposition.value}")
        return receipt

    async def _settle_agent_budget(
        self,
        receipt: AgentBudgetReservationReceipt,
        *,
        actual_tokens: int,
        actual_cost_micro_usd: int,
    ) -> None:
        if self._budget is None:
            raise RuntimeError("Agent budget owner is unavailable")
        await self._budget.settle(
            receipt,
            actual_tokens=actual_tokens,
            actual_cost_micro_usd=actual_cost_micro_usd,
        )

    def _require_lineage_advance(
        self,
        record: LineageRecord,
        target: SpawnLifecycle,
        *,
        placement: str | None = None,
        incarnation_generation: int | None = None,
    ) -> LineageRecord:
        receipt = self._lineage.advance(
            record.request.request_id,
            target,
            expected_revision=record.revision,
            lease=self._lineage_lease,
            placement=placement,
            incarnation_generation=incarnation_generation,
        )
        if (
            receipt.disposition
            not in {
                SpawnAdvanceDisposition.APPLIED,
                SpawnAdvanceDisposition.IDEMPOTENT,
            }
            or receipt.record is None
        ):
            raise RuntimeError(f"durable lineage advance failed: {receipt.disposition.value}")
        return receipt.record

    def _settle_logical_reservation(self, reservation_id: str):
        while True:
            receipt = self._logical_capacity.settle(reservation_id, expected_revision=self._logical_capacity.revision)
            if receipt.disposition is not CapacitySettlementDisposition.REVISION_CONFLICT:
                return receipt

    def _rollback_logical_capacity(self, reservation_id: str) -> None:
        self._settle_logical_reservation(reservation_id)

    @staticmethod
    def _rollback_residency_slot(slot: ResidencySlot) -> None:
        slot.rollback()

    def _discard_logical_reservation(self, agent_id: str) -> None:
        self._logical_reservations.pop(agent_id, None)

    def _discard_budget_reservation(self, agent_id: str) -> None:
        self._budget_reservations.pop(agent_id, None)

    def _resolve_parent_path(self, parent_id: Optional[str]) -> AgentPath:
        """The parent's registered path, or root when unknown/unparented."""
        if parent_id:
            meta = self._registry.agent_metadata_for_id(parent_id)
            if meta is not None and meta.agent_path is not None:
                return meta.agent_path
        return AgentPath.root()

    def _build_spawn_context(self, spec: SpawnPlan[OutputT], child_path: AgentPath) -> SpawnContext:
        """Project stable parent identity and cwd for the child factory."""
        parent_rt = self._runtimes.get(spec.parent_id) if spec.parent_id else None
        if parent_rt is not None:
            return parent_rt.role.build_child_spawn_context(parent_id=spec.parent_id, agent_path=child_path.as_str())
        return SpawnContext(
            parent_id=spec.parent_id,
            agent_path=child_path.as_str(),
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
    def _accept_delivery(self, agent_id: str, message: Message, mode: DeliveryMode):
        return self._delivery_store.accept(
            agent_id,
            message,
            mode.value,
            lease=self._delivery_lease,
        )

    def _bind_deliveries(
        self,
        agent_id: str,
        delivery_ids: tuple[str, ...],
        turn_request_id: str,
        payload_digest: str,
    ) -> None:
        self._delivery_store.bind_to_turn(
            delivery_ids,
            turn_request_id=turn_request_id,
            target_generation=self._current_delivery_generation(agent_id),
            expected_payload_digest=payload_digest,
            lease=self._delivery_lease,
        )

    def _current_delivery_generation(self, agent_id: str) -> int:
        lifecycle = self._residency.lifecycle_snapshot(agent_id)
        if lifecycle is None:
            raise AgentNotFound(agent_id)
        return lifecycle.incarnation_generation

    def current_generation(self, agent_id: str) -> int:
        """Return the canonical active incarnation generation for ingress recovery."""
        return self._current_delivery_generation(agent_id)

    def reconcile_ingress(self) -> AgentIngressReconcileResult:
        if self._turn_queue_store is None or self._turn_queue_lease is None:
            raise RuntimeError("Agent ingress recovery requires the durable Product turn composition")
        return AgentIngressReconciler(
            deliveries=self._delivery_store,
            turns=self._turn_queue_store,
            generations=self,
            delivery_lease=self._delivery_lease,
            turn_lease=self._turn_queue_lease,
        ).reconcile()

    def _ack_deliveries(self, agent_id: str, delivery_ids: tuple[str, ...]) -> None:
        generation = self._current_delivery_generation(agent_id)
        for delivery_id in delivery_ids:
            self._delivery_store.ack(delivery_id, generation, lease=self._delivery_lease)

    def dispatch(self, command: AgentDeliveryCommand) -> AgentDeliveryCommandReceipt:
        """Commit one typed Product command through canonical durable ingress."""
        message = UserMessage(id=command.source_id, content=command.content)
        self.send_input(
            command.target_agent_id,
            message,
            mode=DeliveryMode.TRIGGER_TURN,
        )
        delivery_id = AgentDeliveryStore.identity(command.target_agent_id, message)
        record = next(record for record in self._delivery_store.records() if record.delivery_id == delivery_id)
        disposition = (
            AgentDeliveryCommandDisposition.ALREADY_SETTLED
            if record.state in {AgentDeliveryState.ACKED, AgentDeliveryState.DEAD_LETTER}
            else AgentDeliveryCommandDisposition.ACCEPTED
        )
        return AgentDeliveryCommandReceipt(disposition, delivery_id)

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
        record = self._accept_delivery(agent_id, message, mode)
        if record.state in {AgentDeliveryState.ACKED, AgentDeliveryState.DEAD_LETTER}:
            return runtime
        if runtime is None or (trigger and not self._limiter.has_capacity()):
            self._pending.park(
                agent_id,
                PendingDelivery(
                    delivery_id=record.delivery_id,
                    target_generation=record.target_generation,
                    message=message,
                    mode=mode,
                ),
            )
            return runtime

        def deliver(target: AgentRuntime) -> None:
            target.mailbox.enqueue(message, mode=mode, delivery_id=record.delivery_id)
            if trigger:
                target.wake()

        delivered = self._residency.deliver_if_active(agent_id, deliver)
        if delivered is None:
            self._pending.park(
                agent_id,
                PendingDelivery(
                    delivery_id=record.delivery_id,
                    target_generation=record.target_generation,
                    message=message,
                    mode=mode,
                ),
            )
            return None
        self._record_last_task_message(agent_id, _preview(message))
        return delivered

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
        message = communication.to_message()
        runtime = self._try_load_sync(agent_id)
        trigger = communication.trigger_turn
        mode = DeliveryMode.TRIGGER_TURN if trigger else DeliveryMode.QUEUE_ONLY
        record = self._accept_delivery(agent_id, message, mode)
        if record.state in {AgentDeliveryState.ACKED, AgentDeliveryState.DEAD_LETTER}:
            return runtime
        if runtime is None or (trigger and not self._limiter.has_capacity()):
            self._pending.park(
                agent_id,
                PendingDelivery(
                    delivery_id=record.delivery_id,
                    target_generation=record.target_generation,
                    message=message,
                    mode=mode,
                ),
            )
            return runtime

        def deliver(target: AgentRuntime) -> None:
            target.mailbox.enqueue(message, mode=mode, delivery_id=record.delivery_id)
            if trigger:
                target.wake()

        delivered = self._residency.deliver_if_active(agent_id, deliver)
        if delivered is None:
            self._pending.park(
                agent_id,
                PendingDelivery(
                    delivery_id=record.delivery_id,
                    target_generation=record.target_generation,
                    message=message,
                    mode=mode,
                ),
            )
            return None
        self._record_last_task_message(agent_id, communication.content)
        return delivered

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
        runtime = self._residency.runtime_for_delivery(agent_id)
        if runtime is not None:
            return runtime
        lifecycle = self._residency.lifecycle_snapshot(agent_id)
        if lifecycle is not None and lifecycle.phase is not ResidentLifecyclePhase.EVICTED:
            if lifecycle.phase in {
                ResidentLifecyclePhase.TERMINAL,
                ResidentLifecyclePhase.TOMBSTONED,
                ResidentLifecyclePhase.PURGED,
            }:
                raise AgentNotFound(agent_id)
            return None
        if not self._store.has(agent_id):
            raise AgentNotFound(agent_id)
        generation = self._current_delivery_generation(agent_id)
        self._residency.ensure_evicted(agent_id, generation)
        claim = self._residency.begin_rehydration(agent_id)
        if claim is None:
            return None
        slot = self._residency.try_reserve_sync(self._max_resident_incarnations)
        if slot is None:
            self._residency.abort_rehydration(claim)
            return None  # hard cap, no synchronous room — caller parks
        return self._install_rehydrated(agent_id, slot, claim)

    async def _ensure_loaded_async(self, agent_id: str) -> Optional[AgentRuntime]:
        """Return the live runtime, rehydrating via an *evicting* reservation.

        The asynchronous fulfilment loader: unlike :meth:`_try_load_sync` it may
        ``await`` an LRU eviction (:meth:`Residency.reserve_slot`) to free a
        live-incarnation slot at the hard cap. Returns ``None`` when even an
        eviction cannot free room (every resident busy / protected) — the caller
        leaves the deliveries parked and retries next boundary (back-pressure).
        Raises :class:`AgentNotFound` only when the agent is unknown.
        """
        runtime = self._residency.runtime_for_delivery(agent_id)
        if runtime is not None:
            return runtime
        lifecycle = self._residency.lifecycle_snapshot(agent_id)
        if lifecycle is not None and lifecycle.phase in {
            ResidentLifecyclePhase.DRAINING,
            ResidentLifecyclePhase.EVICTION_RETRY,
        }:
            if not await self._residency.retry_eviction(agent_id):
                return None
            lifecycle = self._residency.lifecycle_snapshot(agent_id)
        if lifecycle is not None and lifecycle.phase is not ResidentLifecyclePhase.EVICTED:
            if lifecycle.phase in {
                ResidentLifecyclePhase.TERMINAL,
                ResidentLifecyclePhase.TOMBSTONED,
                ResidentLifecyclePhase.PURGED,
            }:
                raise AgentNotFound(agent_id)
            return None
        if not self._store.has(agent_id):
            raise AgentNotFound(agent_id)
        generation = self._incarnation_factory.generation(agent_id)
        self._residency.ensure_evicted(agent_id, generation)
        claim = self._residency.begin_rehydration(agent_id)
        if claim is None:
            return None
        try:
            slot = await self._residency.reserve_slot(self._max_resident_incarnations)
        except AgentLimitReached:
            self._residency.abort_rehydration(claim)
            return None  # back-pressure: nothing evictable right now
        # The evicting reservation already holds the freed slot — install on it
        # directly (no rollback-and-re-reserve dance, so no transient window in
        # which a racing reservation could steal the room).
        return self._install_rehydrated(agent_id, slot, claim)

    def _install_rehydrated(
        self,
        agent_id: str,
        slot: ResidencySlot,
        claim: ResidentTransitionClaim,
    ) -> Optional[AgentRuntime]:
        """Rehydrate *agent_id* from disk onto an already-held *slot*.

        Shared body of both loaders: materialize the runtime, register it into
        the live map + scheduler, commit the slot (pending -> resident), and drop
        the on-disk copy. Rolls the slot back (and returns ``None`` / re-raises)
        on any failure, so a reserved slot is never leaked — both loaders hand in
        a :class:`ResidencySlot`, so commit / rollback follow one discipline.
        """
        try:
            identity = self._residency_identity(agent_id)
            factory = self._incarnation_factory.factory(agent_id)
            lease = self._residency_lease(agent_id)
            restored_state = self._store.rehydrate(identity, factory=factory, lease=lease)
        except BaseException:
            slot.rollback()
            self._residency.abort_rehydration(claim)
            raise
        if restored_state is None:
            slot.rollback()
            self._residency.abort_rehydration(claim)
            raise AgentNotFound(agent_id)
        restored_agent = restored_state.agent
        restored_mailbox = restored_state.mailbox
        if not isinstance(restored_agent, RunnableAgent):
            slot.rollback()
            self._residency.abort_rehydration(claim)
            raise TypeError("trusted Residency factory returned a non-runnable Agent")
        restored = AgentRuntime(restored_agent, restored_mailbox)
        installed = False
        generation_advanced = False
        try:
            metadata = self._registry.agent_metadata_for_id(agent_id)
            if metadata is None or metadata.agent_path is None:
                raise RuntimeError("rehydrated Agent lost its canonical registry identity")
            if metadata.agent_id != restored.session_id or restored.session_id != agent_id:
                raise RuntimeError("rehydrated runtime and registry identities differ")
            restored.agent_path = metadata.agent_path
            self._runtimes[agent_id] = restored
            self._scheduler.add_runtime(restored)
            installed = True
            self._incarnation_factory.advance_generation(agent_id, expected=identity.incarnation_generation)
            generation_advanced = True
            self._store.forget(
                agent_id,
                expected_record_revision=restored_state.install_record_revision,
                lease=lease,
            )
            receipt = self._residency.complete_rehydration(
                claim,
                next_generation=identity.incarnation_generation + 1,
            )
            if receipt.disposition.value != "applied":
                raise RuntimeError("rehydration lifecycle claim became stale")
            slot.commit(agent_id, identity.incarnation_generation + 1)
        except BaseException:
            if generation_advanced:
                self._incarnation_factory.rollback_generation(
                    agent_id,
                    expected_current=identity.incarnation_generation + 1,
                    restore=identity.incarnation_generation,
                )
            if installed:
                self._remove_runtime(agent_id)
            slot.rollback()
            self._residency.abort_rehydration(claim)
            raise
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
                self._delivery_store.dead_letter_target(agent_id, "target_not_found", lease=self._delivery_lease)
                self._pending.drop(agent_id)
                continue
            if runtime is None:
                self._note_delivery_back_pressure(agent_id)  # still no room, leave parked
                continue
            deliveries = self._pending.take_all(agent_id)
            for index, delivery in enumerate(deliveries):
                delivered = self._residency.deliver_if_active(
                    agent_id,
                    lambda target, item=delivery: self._deliver_now(target, agent_id, item),
                )
                if delivered is None:
                    for remaining in deliveries[index:]:
                        self._pending.park(agent_id, remaining)
                    break
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
        generation = self._current_delivery_generation(agent_id)
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
            runtime.mailbox.enqueue(message, mode=delivery.mode, delivery_id=delivery.delivery_id)
            if delivery.mode is DeliveryMode.TRIGGER_TURN:
                runtime.wake()
            self._record_last_task_message(agent_id, _preview(message))

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
        if not self._lease_heartbeats_started:
            self._turn_queue_lease_handle.adopt_nowait(self._turn_queue_lease)
            self._lineage_lease_handle.adopt_nowait(self._lineage_lease)
            self._delivery_lease_handle.adopt_nowait(self._delivery_lease)
            self._lease_heartbeats_started = True
        self._start_telemetry()
        self._scheduler.start()
        # Event-driven fulfilment for the fleet-idle case (persistent mode only;
        # bounded ``run(k)`` fulfils inline at each pump round instead).
        if self._pending_waker_task is None or self._pending_waker_task.done():
            self._pending_waker_task = asyncio.create_task(self._pending_waker_loop())

    async def stop(self) -> None:
        if self._workflow_governance_registered:
            assert self._workflow_governance is not None
            self._workflow_governance.unregister_agent_governance(AgentId(self._lineage_root_id))
            self._workflow_governance_registered = False
        if self._workflow_delivery_registered:
            assert self._workflow_delivery is not None
            self._workflow_delivery.unregister_agent_delivery(AgentId(self._lineage_root_id), self)
            self._workflow_delivery_registered = False
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
            try:
                if self._lease_heartbeats_started:
                    await asyncio.gather(
                        self._turn_queue_lease_handle.close(),
                        self._lineage_lease_handle.close(),
                        self._delivery_lease_handle.close(),
                    )
                    self._lease_heartbeats_started = False
                else:
                    for lease in (self._turn_queue_lease, self._lineage_lease, self._delivery_lease):
                        try:
                            self._residency_lease_coordinator.release(lease)
                        except LeaseFencedError:
                            pass
            finally:
                self._turn_queue_lease = None
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
