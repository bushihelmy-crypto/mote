"""Application-owned composition for the canonical durable Workflow plane."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Protocol
from uuid import uuid4

from mote.contracts.agent.runtime_identity import AgentId
from mote.contracts.clock import AbsoluteInstant
from mote.contracts.ports.agent.delivery import (
    AgentDeliveryCommand,
    AgentDeliveryCommandDisposition,
    AgentDeliveryPort,
    AgentDeliverySourceKind,
)
from mote.contracts.ports.workflow.admission import WorkflowCreateAdmissionPort
from mote.contracts.ports.workflow.authority import WorkflowCallerAuthorizationPort
from mote.contracts.ports.workflow.execution import WorkflowNodeExecutionPort
from mote.contracts.ports.workflow.governance import WorkflowGovernanceSnapshotVerifierPort
from mote.contracts.runtime.lease import RuntimeLease, RuntimeLeasePolicy
from mote.contracts.runtime.operation_ownership import (
    EffectCapability,
    EffectSettlement,
    OperationBackend,
    OperationOwnership,
    OperationOwnershipRequest,
)
from mote.contracts.workflow import (
    DeclarativeWorkflowDefinitionSource,
    TrustedWorkflowBlueprintSource,
    WorkflowDefinitionId,
    WorkflowDefinitionSource,
    WorkflowRunId,
    WorkflowRunReference,
)
from mote.contracts.workflow.admission import WorkflowCreateAdmission
from mote.contracts.workflow.authority import WorkflowCallerContext, WorkflowCreateAdmissionId
from mote.contracts.workflow.governance import (
    WorkflowGovernanceAcceptanceDisposition,
    WorkflowGovernanceCancelAcceptance,
    WorkflowGovernanceCancelRequest,
    WorkflowGovernanceCancelSettlementSnapshot,
    WorkflowGovernanceScopeCancelRequestId,
)
from mote.orchestration.workflows import WorkflowExecutable
from mote.orchestration.workflows.creation import WorkflowCreateAdmissionReconciler, WorkflowCreateCoordinator
from mote.orchestration.workflows.durable import (
    CreateWorkflowRun,
    ReconcileState,
    WorkflowEffect,
    WorkflowGovernanceCancellationInbox,
    WorkflowGovernanceCancellationReconciler,
    WorkflowReconciler,
    WorkflowReconciliationStore,
    WorkflowRunControl,
    WorkflowRunProjection,
    WorkflowRunStore,
    WorkflowTerminalDelivery,
)
from mote.orchestration.workflows.observation import BoundWorkflowAsyncWorkAdapter
from mote.product.workflows.run_graph.compiler import build_graph
from mote.product.workflows.run_graph.spec import GraphSpec
from mote.runtime.clock import SystemClock
from mote.runtime.control.leases import FileLeaseCoordinator, LeaseHandle
from mote.runtime.control.operation_ownership import LeaseOperationOwnership
from mote.runtime.telemetry.logging import logger

if TYPE_CHECKING:
    from mote.runtime.durable.temporal._activities import StepInput

EffectExecutor = Callable[[WorkflowEffect], tuple[EffectSettlement, str]]
TrustedBlueprintFactory = Callable[[], WorkflowExecutable]
WorkflowExecutionActivator = Callable[[WorkflowRunProjection], bool]


@dataclass(frozen=True, slots=True)
class TrustedWorkflowBlueprint:
    blueprint_id: str
    blueprint_version: int
    factory: TrustedBlueprintFactory

    def __post_init__(self) -> None:
        TrustedWorkflowBlueprintSource(self.blueprint_id, self.blueprint_version)


@dataclass(frozen=True, slots=True)
class _ActiveExecution:
    task: asyncio.Task[object]
    cancel: Callable[[], Awaitable[None]]


class TemporalEffectPlane(Protocol):
    async def start(self) -> None: ...

    async def execute(self, effect: WorkflowEffect) -> tuple[EffectSettlement, str]: ...

    async def aclose(self) -> None: ...


class ProductWorkflowDurability:
    """The sole Product activation, scan and shutdown owner for Workflow runs."""

    def __init__(self, root: Path, *, scan_interval_seconds: float = 0.25) -> None:
        if scan_interval_seconds <= 0:
            raise ValueError("Workflow reconciliation interval must be positive")
        root.mkdir(parents=True, exist_ok=True)
        lease_coordinator = FileLeaseCoordinator(root / "operation-leases.json")
        ownership = LeaseOperationOwnership(
            lease_coordinator,
            backend=OperationBackend.LOCAL_FILE,
        )
        self._lease_coordinator = lease_coordinator
        holder_id = f"product-workflow:{uuid4().hex}"
        deployment_id = f"local-workspace:{root.resolve()}"
        self._ownership = ownership
        self._holder_id = holder_id
        self._deployment_id = deployment_id
        self._runs = WorkflowRunStore(root / "runs.json", ownership)
        self.control = WorkflowRunControl(
            self._runs,
            ownership,
            deployment_id=deployment_id,
            holder_id=holder_id,
            backend=OperationBackend.LOCAL_FILE,
        )
        reconciliation_store = WorkflowReconciliationStore(root / "reconciliation.json", ownership)
        self._reconciliation_store = reconciliation_store
        self.reconciler = WorkflowReconciler(
            reconciliation_store,
            ownership,
            deployment_id=deployment_id,
            holder_id=holder_id,
            backend=OperationBackend.LOCAL_FILE,
            now=SystemClock().now,
        )
        self._effect_executors: dict[str, EffectExecutor] = {}
        self._agent_deliveries: dict[AgentId, AgentDeliveryPort] = {}
        self._scan_interval_seconds = scan_interval_seconds
        self._scan_task: asyncio.Task[None] | None = None
        self._temporal_effects: TemporalEffectPlane | None = None
        self._execution_tasks: dict[str, _ActiveExecution] = {}
        self._execution_lease_ttl_seconds = 30.0
        self._execution_cancel_timeout_seconds = 5.0
        self._governance_sources: dict[
            AgentId,
            tuple[WorkflowGovernanceSnapshotVerifierPort, WorkflowCreateAdmissionPort],
        ] = {}
        self._trusted_blueprints: dict[tuple[str, int], TrustedBlueprintFactory] = {}
        self._execution_activators: dict[AgentId, WorkflowExecutionActivator] = {}

    def register_agent_governance(
        self,
        root_agent_id: AgentId,
        verifier: WorkflowGovernanceSnapshotVerifierPort,
        admissions: WorkflowCreateAdmissionPort,
    ) -> None:
        if root_agent_id in self._governance_sources:
            raise RuntimeError("Workflow governance root is already registered")
        self._governance_sources[root_agent_id] = (verifier, admissions)

    def unregister_agent_governance(self, root_agent_id: AgentId) -> None:
        if self._governance_sources.pop(root_agent_id, None) is None:
            raise RuntimeError("Workflow governance root is not registered")

    def register_execution_activator(self, agent_id: AgentId, activator: WorkflowExecutionActivator) -> None:
        if agent_id in self._execution_activators:
            raise RuntimeError("Workflow execution activator is already registered")
        self._execution_activators[agent_id] = activator

    def unregister_execution_activator(self, agent_id: AgentId) -> None:
        if self._execution_activators.pop(agent_id, None) is None:
            raise RuntimeError("Workflow execution activator is not registered")

    def submit(self, request: WorkflowGovernanceCancelRequest) -> WorkflowGovernanceCancelAcceptance:
        source = self._governance_sources.get(request.root_agent_id)
        if source is None:
            return WorkflowGovernanceCancelAcceptance(
                request.request_id,
                WorkflowGovernanceAcceptanceDisposition.FENCE_LOST,
                None,
                len(request.target_agent_ids),
            )
        return WorkflowGovernanceCancellationInbox(self._reconciliation_store, source[0]).submit(request)

    def get(
        self, request_id: WorkflowGovernanceScopeCancelRequestId
    ) -> WorkflowGovernanceCancelSettlementSnapshot | None:
        return self._reconciliation_store.get(request_id)

    def get_workflow_create_admission(self, admission_id: WorkflowCreateAdmissionId) -> WorkflowCreateAdmission | None:
        matches = tuple(
            admission
            for _verifier, admissions in self._governance_sources.values()
            if (admission := admissions.get_workflow_create_admission(admission_id)) is not None
        )
        if len(matches) > 1:
            raise RuntimeError("Workflow admission identity has multiple owners")
        return matches[0] if matches else None

    def attach_temporal_effect_plane(self, plane: TemporalEffectPlane) -> None:
        if self._scan_task is not None or self._temporal_effects is not None:
            raise RuntimeError("Temporal Workflow effect plane is already selected")
        self._temporal_effects = plane

    def create_for_agent(
        self,
        command: CreateWorkflowRun,
        caller: WorkflowCallerContext,
        admissions: WorkflowCreateAdmissionPort,
    ) -> WorkflowRunProjection:
        return WorkflowCreateCoordinator(
            self.control,
            admissions,
            self._ownership,
            deployment_id=self._deployment_id,
            holder_id=self._holder_id,
            backend=OperationBackend.LOCAL_FILE,
        ).create(command, caller)

    def register_trusted_blueprint(
        self,
        blueprint_id: str,
        blueprint_version: int,
        factory: TrustedBlueprintFactory,
    ) -> None:
        source = TrustedWorkflowBlueprintSource(blueprint_id, blueprint_version)
        key = (source.blueprint_id, source.blueprint_version)
        if key in self._trusted_blueprints:
            raise RuntimeError("trusted Workflow blueprint is already registered")
        executable = factory()
        if not isinstance(executable, WorkflowExecutable):
            raise TypeError("trusted Workflow blueprint factory returned another type")
        self._trusted_blueprints[key] = factory

    def resolve_definition_source(
        self,
        source: WorkflowDefinitionSource,
        *,
        expected_definition_id: WorkflowDefinitionId,
        expected_digest: str,
        workflow_nodes: WorkflowNodeExecutionPort,
    ) -> WorkflowExecutable:
        if isinstance(source, TrustedWorkflowBlueprintSource):
            factory = self._trusted_blueprints.get((source.blueprint_id, source.blueprint_version))
            if factory is None:
                raise KeyError("trusted Workflow blueprint is not activated")
            executable = factory()
        elif isinstance(source, DeclarativeWorkflowDefinitionSource):
            if source.compiler_id != "mote.product.run-graph" or source.compiler_version != 1:
                raise ValueError("declarative Workflow compiler is not activated")
            spec = GraphSpec.model_validate_json(source.payload)
            executable = build_graph(
                spec,
                dispatch=workflow_nodes.dispatch,
                command_name="RunGraph",
                valid_tools=set(workflow_nodes.allowed_tool_names()),
            ).build()
        else:
            raise TypeError("unknown Workflow definition source")
        definition = executable.definition
        if definition.definition_id != expected_definition_id or definition.digest != expected_digest:
            raise ValueError("activated Workflow definition identity mismatch")
        return executable

    def query(self, reference: WorkflowRunReference) -> WorkflowRunProjection | None:
        return self._runs.get(reference)

    def bind_async_work(
        self,
        caller: WorkflowCallerContext,
        authorizer: WorkflowCallerAuthorizationPort,
        execution_ownership: OperationOwnership | None = None,
    ) -> BoundWorkflowAsyncWorkAdapter:
        return BoundWorkflowAsyncWorkAdapter(
            caller,
            authorizer,
            self,
            self._runs,
            self._reconciliation_store,
            self.control,
            execution_ownership,
        )

    def schedule_execution(
        self,
        run_id: WorkflowRunId,
        execute: Callable[[OperationOwnership], Awaitable[object]],
        cancel: Callable[[], Awaitable[None]],
        *,
        ownership: OperationOwnership | None = None,
    ) -> None:
        """Activate one process incarnation of a durable Workflow run.

        The task is an application-owned executor projection; authoritative
        lifecycle remains in WorkflowRunStore. BackgroundTaskPool is not part
        of this path.
        """
        if not run_id or run_id in self._execution_tasks:
            raise RuntimeError("Workflow execution is already active")
        ownership = ownership or self.claim_execution(run_id)

        async def drive() -> object:
            lease_handle = LeaseHandle(
                self._lease_coordinator,
                subject=ownership.subject,
                owner_id=ownership.request.holder_id,
                policy=RuntimeLeasePolicy(
                    ttl_seconds=self._execution_lease_ttl_seconds,
                    renew_interval_seconds=self._execution_lease_ttl_seconds / 3,
                ),
            )
            await lease_handle.adopt(
                RuntimeLease(
                    ownership.subject,
                    ownership.request.holder_id,
                    ownership.fencing_token,
                    ownership.expires_at,
                )
            )
            execution_task: asyncio.Task[object] | None = None
            ownership_loss = asyncio.create_task(
                lease_handle.wait_for_loss(),
                name=f"mote-workflow-lease:{run_id}",
            )
            try:

                async def invoke() -> object:
                    return await execute(ownership)

                execution_task = asyncio.create_task(invoke())
                done, _pending = await asyncio.wait(
                    (execution_task, ownership_loss),
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if ownership_loss in done:
                    failure = ownership_loss.exception()
                    if failure is not None:
                        await cancel()
                        await asyncio.gather(execution_task, return_exceptions=True)
                        raise RuntimeError("Workflow execution lease heartbeat failed") from failure
                return await execution_task
            finally:
                ownership_loss.cancel()
                await asyncio.gather(ownership_loss, return_exceptions=True)
                if execution_task is not None and not execution_task.done():
                    execution_task.cancel()
                    await asyncio.gather(execution_task, return_exceptions=True)
                await lease_handle.close()

        task = asyncio.create_task(drive(), name=f"mote-workflow:{run_id}")
        self._execution_tasks[run_id] = _ActiveExecution(task, cancel)

        def retire(completed: asyncio.Task[object]) -> None:
            active = self._execution_tasks.get(run_id)
            if active is not None and active.task is completed:
                self._execution_tasks.pop(run_id, None)
            if not completed.cancelled():
                completed.exception()

        task.add_done_callback(retire)

    def claim_execution(self, run_id: WorkflowRunId) -> OperationOwnership:
        operation_id = f"workflow-execution:{run_id}"
        return self._ownership.claim(
            OperationOwnershipRequest(
                self._deployment_id,
                operation_id,
                self._holder_id,
                OperationBackend.LOCAL_FILE,
                0,
                operation_id,
                EffectCapability.NO_EXTERNAL_EFFECT,
            ),
            self._execution_lease_ttl_seconds,
        )

    def release_execution(self, ownership: OperationOwnership) -> None:
        self._ownership.release(ownership)

    def assert_execution_current(self, run_id: WorkflowRunId, ownership: OperationOwnership) -> None:
        if ownership.request.operation_id != f"workflow-execution:{run_id}":
            raise RuntimeError("Workflow execution ownership binds another run")
        self._ownership.assert_current(ownership)

    def pending_terminal_destinations(self, prefix: str) -> tuple[str, ...]:
        return self.reconciler.pending_terminal_destinations(prefix)

    def register_effect_executor(self, handler_id: str, executor: EffectExecutor) -> None:
        if not handler_id or handler_id in self._effect_executors:
            raise ValueError("Workflow effect handler identity is invalid or duplicated")
        self._effect_executors[handler_id] = executor

    def register_agent_delivery(self, agent_id: AgentId, delivery: AgentDeliveryPort) -> None:
        if agent_id in self._agent_deliveries:
            raise ValueError("Workflow Agent delivery owner is already registered")
        self._agent_deliveries[agent_id] = delivery

    def unregister_agent_delivery(self, agent_id: AgentId, delivery: AgentDeliveryPort) -> None:
        if self._agent_deliveries.get(agent_id) is not delivery:
            raise ValueError("Workflow Agent delivery owner is stale or foreign")
        del self._agent_deliveries[agent_id]

    async def start(self) -> None:
        if self._scan_task is not None:
            raise RuntimeError("Workflow durability is already active")
        if self._temporal_effects is not None:
            await self._temporal_effects.start()
        self._scan_task = asyncio.create_task(self._scan_loop(), name="mote-workflow-reconciler")

    async def aclose(self) -> None:
        execution_tasks = tuple(active.task for active in self._execution_tasks.values())
        self._execution_tasks.clear()
        for execution_task in execution_tasks:
            execution_task.cancel()
        if execution_tasks:
            await asyncio.gather(*execution_tasks, return_exceptions=True)
        task = self._scan_task
        self._scan_task = None
        if task is None:
            if self._temporal_effects is not None:
                await self._temporal_effects.aclose()
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        if self._temporal_effects is not None:
            await self._temporal_effects.aclose()

    async def _scan_loop(self) -> None:
        while True:
            for _verifier, admissions in tuple(self._governance_sources.values()):
                WorkflowCreateAdmissionReconciler(
                    admissions,
                    self._runs,
                    self._ownership,
                    holder_id=self._holder_id,
                ).reconcile()
            if self._governance_sources:
                WorkflowGovernanceCancellationReconciler(
                    self._reconciliation_store,
                    self._ownership,
                    self._runs,
                    self.control,
                    self,
                    deployment_id=self._deployment_id,
                    holder_id=self._holder_id,
                    backend=OperationBackend.LOCAL_FILE,
                    now=SystemClock().now,
                ).reconcile()
            await self._reconcile_execution_cancellations()
            self._reactivate_nonterminal_runs()
            if self._temporal_effects is None:
                self.reconciler.reconcile_effects(
                    self._execute_effect,
                    eligible=self._effect_is_activated,
                )
            else:
                await self.reconciler.reconcile_effects_async(
                    self._temporal_effects.execute,
                    eligible=self._effect_is_activated,
                )
            self.reconciler.reconcile_deliveries(
                self._deliver_terminal,
                eligible=self._delivery_is_activated,
            )
            self._reconcile_effect_retention()
            await asyncio.sleep(self._scan_interval_seconds)

    def _reconcile_effect_retention(self) -> None:
        """Product-owned bounded retention maintenance for terminal effects."""
        now = SystemClock().now()
        terminal_before = AbsoluteInstant(1, now.clock, now.epoch_nanoseconds - 90 * 86_400 * 1_000_000_000)
        tombstone_before = AbsoluteInstant(1, now.clock, now.epoch_nanoseconds - 365 * 86_400 * 1_000_000_000)
        for effect_id in self._reconciliation_store.scan_effect_retention(before=terminal_before, limit=500):
            self._reconciliation_store.tombstone_effect(effect_id, before=terminal_before, now=now)
        for effect_id, tombstone in tuple(self._reconciliation_store.records()["tombstones"].items()):
            if tombstone.tombstoned_at.epoch_nanoseconds <= tombstone_before.epoch_nanoseconds:
                self._reconciliation_store.purge_effect_tombstone(effect_id, before=now)

    def _reactivate_nonterminal_runs(self) -> None:
        eligible = {
            "created",
            "running",
            "cancelling",
        }
        for projection in self._runs.scan():
            if projection.phase.value not in eligible or projection.reference.run_id in self._execution_tasks:
                continue
            activator = self._execution_activators.get(projection.provenance.creator_logical_agent_id)
            if activator is not None:
                try:
                    activator(projection)
                except (KeyError, TypeError, ValueError, RuntimeError) as exc:
                    logger.error(f"Workflow {projection.reference.run_id} " f"reactivation rejected: {exc}")

    async def _reconcile_execution_cancellations(self) -> None:
        cancelling = {
            projection.reference.run_id for projection in self._runs.scan() if projection.phase.value == "cancelling"
        }
        for run_id in cancelling:
            active = self._execution_tasks.get(run_id)
            if active is not None and not active.task.done():
                try:
                    await asyncio.wait_for(
                        active.cancel(),
                        timeout=self._execution_cancel_timeout_seconds,
                    )
                except TimeoutError:
                    continue

    def _effect_is_activated(self, effect: WorkflowEffect) -> bool:
        return effect.command_payload.partition("\n")[0] in self._effect_executors

    async def dispatch_temporal_effect(self, command: StepInput) -> str:
        if command.kind != "workflow_effect":
            raise ValueError("Temporal Workflow effect kind is invalid")
        raw = json.loads(command.payload)
        fields = {
            "schema",
            "effect_id",
            "run_id",
            "capability",
            "command_payload",
            "provider_receipt",
            "state",
            "revision",
            "attempts",
            "next_eligible_at",
            "reason",
        }
        if type(raw) is not dict or set(raw) != fields:
            raise ValueError("Temporal Workflow effect command is invalid")
        string_fields = {
            "schema",
            "effect_id",
            "run_id",
            "capability",
            "command_payload",
            "provider_receipt",
            "state",
            "reason",
        }
        if any(type(raw[field]) is not str for field in string_fields):
            raise ValueError("Temporal Workflow effect string primitive is invalid")
        if raw["schema"] != "mote.workflow-effect-command/v1":
            raise ValueError("Temporal Workflow effect schema is unknown")
        if (
            not raw["effect_id"]
            or not raw["run_id"]
            or raw["effect_id"] != command.effect_id
            or raw["effect_id"] != command.step_id
            or raw["capability"] != command.effect_capability.value
        ):
            raise ValueError("Temporal Workflow effect identity binding is invalid")
        if (
            type(raw["revision"]) is not int
            or raw["revision"] < 1
            or type(raw["attempts"]) is not int
            or raw["attempts"] < 0
            or type(raw["next_eligible_at"]) is not dict
        ):
            raise ValueError("Temporal Workflow effect counter/instant is invalid")
        effect = WorkflowEffect(
            raw["effect_id"],
            raw["run_id"],
            EffectCapability(raw["capability"]),
            raw["command_payload"],
            raw["provider_receipt"],
            ReconcileState(raw["state"]),
            raw["revision"],
            raw["attempts"],
            AbsoluteInstant.from_dict(raw["next_eligible_at"]),
            raw["reason"],
        )
        settlement, receipt = self._execute_effect(effect)
        return json.dumps(
            {"settlement": settlement.value, "receipt": receipt},
            sort_keys=True,
            separators=(",", ":"),
        )

    def _execute_effect(self, effect: WorkflowEffect) -> tuple[EffectSettlement, str]:
        try:
            handler_id, _separator, _payload = effect.command_payload.partition("\n")
            executor = self._effect_executors[handler_id]
        except KeyError as exc:
            raise RuntimeError("Workflow effect handler is not activated") from exc
        return executor(effect)

    def _deliver_terminal(self, delivery: WorkflowTerminalDelivery) -> bool:
        agent_id = self._delivery_agent_id(delivery)
        try:
            port = self._agent_deliveries[agent_id]
        except KeyError as exc:
            raise RuntimeError("Workflow terminal Agent delivery owner is not activated") from exc
        receipt = port.dispatch(
            AgentDeliveryCommand(
                AgentDeliverySourceKind.WORKFLOW,
                delivery.delivery_id,
                str(agent_id),
                f"<workflow-result run_id={delivery.run_id!r}>{delivery.outcome_payload}</workflow-result>",
            )
        )
        return receipt.disposition in {
            AgentDeliveryCommandDisposition.ACCEPTED,
            AgentDeliveryCommandDisposition.ALREADY_SETTLED,
        }

    def _delivery_is_activated(self, delivery: WorkflowTerminalDelivery) -> bool:
        try:
            agent_id = self._delivery_agent_id(delivery)
        except ValueError:
            return False
        return agent_id in self._agent_deliveries

    @staticmethod
    def _delivery_agent_id(delivery: WorkflowTerminalDelivery) -> AgentId:
        prefix = "agent:"
        marker = ":workflow:"
        if not delivery.destination_id.startswith(prefix) or marker not in delivery.destination_id:
            raise ValueError("Workflow terminal destination is not a canonical Agent destination")
        agent, separator, run_id = delivery.destination_id[len(prefix) :].partition(marker)
        if not separator or not agent or run_id != delivery.run_id:
            raise ValueError("Workflow terminal destination identity is inconsistent")
        return AgentId(agent)


__all__ = [
    "ProductWorkflowDurability",
    "TemporalEffectPlane",
    "TrustedWorkflowBlueprint",
]
