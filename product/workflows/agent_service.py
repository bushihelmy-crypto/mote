"""Per-Agent Product capability for durable Workflow submission and inspection."""

import json
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import cast

from mote.contracts.agent.runtime_identity import AgentId
from mote.contracts.async_work import (
    AsyncWorkReference,
    CancelDurableWorkflowRun,
    DurableWorkflowRunObservation,
    DurableWorkflowRunReference,
    DurableWorkflowRunSubmission,
    ResumeDurableWorkflowRun,
    WorkflowCancelReceipt,
    WorkflowResumeDisposition,
)
from mote.contracts.events.envelope import JsonValue, freeze_json
from mote.contracts.ports.artifact.store import ReliableArtifactPublisher
from mote.contracts.ports.async_work.observation import AsyncWorkQueryDisposition, AsyncWorkQueryResult
from mote.contracts.ports.task.operations import BackgroundTaskService
from mote.contracts.ports.workflow.execution import WorkflowNodeExecutionPort
from mote.contracts.runtime.operation_ownership import OperationOwnership
from mote.contracts.session.identity import SessionId
from mote.contracts.workflow import (
    WorkflowCreateAdmissionId,
    WorkflowDefinitionId,
    WorkflowRunAccessGrant,
    WorkflowRunCreationProvenance,
    WorkflowRunId,
    WorkflowRunReference,
)
from mote.contracts.workflow.command import WorkflowCancelReason
from mote.orchestration.agents.control_context import resolve_workflow_caller_control
from mote.orchestration.workflows.deferred import WorkflowDeferredResult, WorkflowRunMetadata
from mote.orchestration.workflows.durable import CreateWorkflowRun
from mote.orchestration.workflows.types import GraphPause, GraphRunState, GraphState
from mote.product.async_work.service import CurrentAgentAsyncWorkCommandService, CurrentAgentAsyncWorkObservationService
from mote.product.workflows.durability import ProductWorkflowDurability
from mote.product.workflows.execution_adapter import WorkflowExecutionAdapter
from mote.product.workflows.inspection import WorkflowGraphView, WorkflowNodeView, WorkflowRunView
from mote.runtime.clock import SystemClock
from mote.runtime.tools.execution_context import current_tool_call_id


@dataclass(frozen=True, slots=True)
class WorkflowResumePlan:
    from_nodes: tuple[str, ...] = ()
    skip_nodes: tuple[str, ...] = ()
    overrides: dict[str, object] | None = None


class AgentWorkflowService:
    """Owns no Workflow state; it composes canonical owners for one Agent."""

    def __init__(
        self,
        durability: ProductWorkflowDurability,
        local_tasks: BackgroundTaskService,
        artifact_publisher: ReliableArtifactPublisher,
        workflow_nodes: WorkflowNodeExecutionPort,
    ) -> None:
        self._durability = durability
        self._artifact_publisher = artifact_publisher
        self._workflow_nodes = workflow_nodes
        self._session_id = local_tasks.session_id
        self._agent_id = AgentId(str(self._session_id))
        self._local_async_work = local_tasks.async_work_adapter()
        self._durability.register_execution_activator(self._agent_id, self._reactivate)

    def submit(self, deferred: WorkflowDeferredResult[GraphState | GraphPause]) -> DurableWorkflowRunSubmission:
        if deferred.graph_meta is None:
            raise ValueError("background Workflow requires durable graph metadata")
        run, projection = self._workflow_run(deferred.graph_meta)
        operation = WorkflowExecutionAdapter(run, self._durability, projection, self._artifact_publisher)
        reference = projection.reference
        self._bind_delivery(reference, operation)
        self._durability.schedule_execution(reference.run_id, operation.execute, operation.aclose)
        return DurableWorkflowRunSubmission(DurableWorkflowRunReference(reference), projection.revision)

    def view(self, reference: WorkflowRunReference) -> WorkflowRunView | None:
        result = self._async_work_service().get(DurableWorkflowRunReference(reference))
        if result.disposition is not AsyncWorkQueryDisposition.FOUND:
            return None
        projection = self._durability.query(reference)
        if projection is None:
            return None
        executable = self._durability.resolve_definition_source(
            projection.definition_source,
            expected_definition_id=projection.reference.definition_id,
            expected_digest=projection.definition_digest,
            workflow_nodes=self._workflow_nodes,
        )
        definition = executable.definition
        initial = json.loads(projection.initial_input_payload)
        if projection.checkpoint_payload == "{}":
            state = executable._graph.state_schema(**initial)
            run_state = GraphRunState.for_graph(executable._graph)
        else:
            state, run_state = executable.restore_checkpoint(projection.checkpoint_payload)
        frozen_state = freeze_json(state.model_dump(mode="json"), path="workflow inspection state")
        if not isinstance(frozen_state, Mapping):
            raise TypeError("Workflow inspection state must be an object")
        graph = executable._graph
        graph_view = WorkflowGraphView(
            nodes=MappingProxyType(
                {
                    name: WorkflowNodeView(
                        name,
                        node.description,
                        MappingProxyType(dict(node.params)),
                    )
                    for name, node in graph._nodes.items()
                }
            ),
            self_loop_nodes=frozenset(name for name in graph._nodes if graph.is_self_loop(name)),
        )
        return WorkflowRunView(
            reference,
            run_state,
            cast(Mapping[str, JsonValue], frozen_state),
            graph_view,
            status=projection.phase,
        )

    def observe(self, reference: AsyncWorkReference) -> AsyncWorkQueryResult:
        """Project and emit either current-Agent async-work variant."""
        return self._async_work_service().get(reference)

    def cancel(
        self,
        reference: WorkflowRunReference,
        *,
        expected_revision: int,
    ) -> WorkflowCancelReceipt:
        return CurrentAgentAsyncWorkCommandService(self._local_async_work, self._bound_async_work()).cancel(
            CancelDurableWorkflowRun(
                DurableWorkflowRunReference(reference),
                expected_revision,
                WorkflowCancelReason.AGENT_REQUEST,
            )
        )

    def resume(
        self,
        reference: WorkflowRunReference,
        deferred: WorkflowDeferredResult[GraphState | GraphPause],
    ) -> WorkflowRunReference:
        if deferred.graph_meta is None:
            raise ValueError("Workflow resume requires durable graph metadata")
        if deferred.graph_meta.executable is not None:
            raise ValueError("Workflow resume cannot accept a live graph continuation")
        observed = self._async_work_service().get(DurableWorkflowRunReference(reference))
        if observed.disposition is not AsyncWorkQueryDisposition.FOUND or not isinstance(
            observed.observation, DurableWorkflowRunObservation
        ):
            raise RuntimeError(f"Workflow resume observation rejected: {observed.disposition.value}")
        pause = observed.observation.detail.pause
        if pause is None:
            raise RuntimeError("Workflow run is not paused")
        execution_ownership = self._durability.claim_execution(reference.run_id)
        scheduled = False
        try:
            durable_projection = self._durability.query(reference)
            if durable_projection is None:
                raise RuntimeError("Workflow resume projection is unavailable")
            definition = self._durability.resolve_definition_source(
                durable_projection.definition_source,
                expected_definition_id=reference.definition_id,
                expected_digest=durable_projection.definition_digest,
                workflow_nodes=self._workflow_nodes,
            )
            metadata = deferred.graph_meta
            # LangGraph-style resume: the durable projection owns checkpoint
            # and pending frontier.  Deferred metadata is presentation-only.
            checkpoint_payload = durable_projection.checkpoint_payload
            frontier = durable_projection.frontier
            receipt = self._bound_async_work(execution_ownership).resume(
                ResumeDurableWorkflowRun(
                    DurableWorkflowRunReference(reference),
                    observed.observation.revision,
                    pause.resume_nonce,
                    checkpoint_payload,
                    tuple(frontier),
                )
            )
            if receipt.disposition is not WorkflowResumeDisposition.RESUMED:
                raise RuntimeError(f"Workflow resume rejected: {receipt.disposition.value}")
            projection = self._durability.query(reference)
            if projection is None or projection.revision != receipt.revision:
                raise RuntimeError("Workflow resume projection is unavailable")
            run = self._restore_from_projection(projection)
            operation = WorkflowExecutionAdapter(run, self._durability, projection, self._artifact_publisher)
            self._bind_delivery(reference, operation)
            self._durability.schedule_execution(
                reference.run_id,
                operation.execute,
                operation.aclose,
                ownership=execution_ownership,
            )
            scheduled = True
        finally:
            if not scheduled:
                self._durability.release_execution(execution_ownership)
        return reference

    async def resume_plan(self, reference: WorkflowRunReference, plan: WorkflowResumePlan) -> WorkflowRunReference:
        """Resolve the approved definition and build continuation inside Product composition."""
        projection = self._durability.query(reference)
        if projection is None:
            raise KeyError(reference)
        executable = self._durability.resolve_definition_source(
            projection.definition_source,
            expected_definition_id=reference.definition_id,
            expected_digest=projection.definition_digest,
            workflow_nodes=self._workflow_nodes,
        )
        initial = json.loads(projection.initial_input_payload)
        if projection.checkpoint_payload == "{}":
            state = executable._graph.state_schema(**initial)
            run_state = GraphRunState.for_graph(executable._graph)
        else:
            state, run_state = executable.restore_checkpoint(projection.checkpoint_payload)
        if plan.overrides:
            valid_fields = set(type(state).model_fields)
            unknown = set(plan.overrides) - valid_fields
            if unknown:
                raise ValueError(f"unknown Workflow override fields: {sorted(unknown)}")
            state = type(state).model_validate(
                {**state.model_dump(mode="python"), **plan.overrides},
                strict=True,
            )
        graph = executable._graph
        for name in plan.from_nodes + plan.skip_nodes:
            if name not in graph._nodes:
                raise ValueError(f"unknown Workflow node: {name}")
        if plan.skip_nodes and plan.from_nodes:
            deferred = graph.resume_skip_and_from(
                state=state,
                skip_nodes=list(plan.skip_nodes),
                from_nodes=list(plan.from_nodes),
                run_state=run_state,
            )
        elif plan.skip_nodes:
            deferred = graph.resume_skip(state=state, skip_nodes=list(plan.skip_nodes), run_state=run_state)
        elif plan.from_nodes:
            deferred = graph.resume(state=state, from_nodes=list(plan.from_nodes), run_state=run_state)
        else:
            deferred = await executable.compile()(**{**initial, **(plan.overrides or {})})
        return self.resume(reference, deferred)

    def _workflow_run(self, graph_meta):
        live_executable = graph_meta.executable
        if live_executable is None:
            raise ValueError("Workflow metadata requires an executable")
        live_definition = live_executable.definition
        source = graph_meta.definition_source
        if source is None:
            raise ValueError("durable Workflow requires a declarative source or trusted blueprint")
        executable = self._durability.resolve_definition_source(
            source,
            expected_definition_id=live_definition.definition_id,
            expected_digest=live_definition.digest,
            workflow_nodes=self._workflow_nodes,
        )
        definition = executable.definition
        request_id = graph_meta.request_id or current_tool_call_id()
        if not request_id:
            raise ValueError("durable Workflow create request identity is required")
        create_request_id = f"{self._session_id}:{request_id}"
        control = resolve_workflow_caller_control()
        caller = control.workflow_caller_context(AgentId(str(self._session_id)))
        admission_id = WorkflowCreateAdmissionId.derive(
            caller.logical_agent_id, create_request_id, definition.definition_id
        )
        provenance = WorkflowRunCreationProvenance(
            admission_id,
            caller.logical_agent_id,
            caller.incarnation_generation,
            caller.lineage_revision,
            caller.cancellation_epoch,
            self._session_id,
            caller.root_governance_agent_id,
            SystemClock().now(),
        )
        initial_input_payload = json.dumps(
            graph_meta.initial_params or {},
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        projection = self._durability.create_for_agent(
            CreateWorkflowRun(
                create_request_id,
                definition.definition_id,
                provenance,
                WorkflowRunAccessGrant(caller.logical_agent_id, caller.root_governance_agent_id),
                source,
                definition.digest,
                initial_input_payload,
            ),
            caller,
            control,
        )
        run = self._restore_from_projection(projection, executable=executable)
        return run, projection

    def _restore_from_projection(self, projection, *, executable=None):
        if executable is None:
            executable = self._durability.resolve_definition_source(
                projection.definition_source,
                expected_definition_id=projection.reference.definition_id,
                expected_digest=projection.definition_digest,
                workflow_nodes=self._workflow_nodes,
            )
        initial = json.loads(projection.initial_input_payload)
        if projection.checkpoint_payload == "{}":
            return executable.start(initial)
        checkpoint, run_state = executable.restore_checkpoint(projection.checkpoint_payload)
        return executable.start(
            initial,
            checkpoint=checkpoint,
            run_state=run_state,
            from_nodes=projection.frontier,
            skip_nodes=tuple(sorted(run_state.completed_names() - set(projection.frontier))),
        )

    def _reactivate(self, projection) -> bool:
        if projection.provenance.creator_logical_agent_id != self._agent_id:
            raise RuntimeError("Workflow reactivation routed to another Agent")
        run = self._restore_from_projection(projection)
        operation = WorkflowExecutionAdapter(run, self._durability, projection, self._artifact_publisher)
        self._bind_delivery(projection.reference, operation)
        self._durability.schedule_execution(projection.reference.run_id, operation.execute, operation.aclose)
        return True

    def _bound_async_work(self, execution_ownership: OperationOwnership | None = None):
        control = resolve_workflow_caller_control()
        caller = control.workflow_caller_context(AgentId(str(self._session_id)))
        return self._durability.bind_async_work(caller, control, execution_ownership)

    def _async_work_service(self) -> CurrentAgentAsyncWorkObservationService:
        return CurrentAgentAsyncWorkObservationService(self._local_async_work, self._bound_async_work())

    def _bind_delivery(self, reference: WorkflowRunReference, operation: WorkflowExecutionAdapter) -> None:
        destination_id = f"agent:{self._session_id}:workflow:{reference.run_id}"
        operation.bind_terminal_destination(destination_id)

    async def aclose(self) -> None:
        self._durability.unregister_execution_activator(self._agent_id)


__all__ = ["AgentWorkflowService"]
