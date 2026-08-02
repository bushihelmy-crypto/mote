from __future__ import annotations

import asyncio
import hashlib
from typing import Annotated

import pytest

from mote.contracts.agent.runtime_identity import AgentId, CancellationEpoch, IncarnationGeneration, LineageRevision
from mote.contracts.artifact import ArtifactRef, ArtifactRevision
from mote.contracts.clock import UNIX_UTC_CLOCK, AbsoluteInstant
from mote.contracts.runtime.errors import LeaseFencedError
from mote.contracts.session.identity import SessionId
from mote.contracts.task.progress import ActivityProgressEvent, ActivityProgressIdentity, ProgressPhase
from mote.contracts.workflow import (
    TrustedWorkflowBlueprintSource,
    WorkflowCreateAdmissionId,
    WorkflowDefinitionId,
    WorkflowNodeDispatchResult,
    WorkflowRunAccessGrant,
    WorkflowRunCreationProvenance,
    WorkflowRunId,
    WorkflowSucceededArtifact,
)
from mote.orchestration.workflows import Output, Paused, Succeeded, WorkflowBuilder
from mote.orchestration.workflows.durable import CreateWorkflowRun, WorkflowRunCommand, WorkflowRunPhase
from mote.orchestration.workflows.types import END, START, GraphState, Stage
from mote.product.workflows.agent_service import AgentWorkflowService
from mote.product.workflows.durability import ProductWorkflowDurability
from mote.product.workflows.execution_adapter import WorkflowExecutionAdapter
from mote.product.workflows.run_graph.compiler import build_graph
from mote.product.workflows.run_graph.spec import GraphSpec


class _Artifacts:
    def __init__(self) -> None:
        self.requests = []

    async def publish(self, publication_id, request):
        self.requests.append((publication_id, request))
        content = request.representations[0].content
        ref = ArtifactRef(
            artifact_id="workflow-result",
            revision=1,
            representation="canonical",
            kind="workflow_result",
            mime_type="application/json",
            content_ref="cas:workflow-result",
            digest=hashlib.sha256(content).hexdigest(),
            size=len(content),
        )
        return ArtifactRevision(ref.artifact_id, ref.revision, (ref,))

    async def publish_intent(self, intent):
        raise AssertionError("not used")

    async def reconcile_pending(self, limit=100):
        raise AssertionError("not used")


class _FailingArtifacts(_Artifacts):
    async def publish(self, publication_id, request):
        self.requests.append((publication_id, request))
        raise OSError("artifact store unavailable")


class _Messages:
    def __init__(self) -> None:
        self.items = []

    def push(self, message) -> None:
        self.items.append(message)


class _LocalTasks:
    def __init__(self, session_id: SessionId) -> None:
        self.session_id = session_id
        self.message_sink = _Messages()

    def async_work_adapter(self):
        return object()


class _WorkflowNodes:
    async def dispatch(self, tool_name, arguments):
        assert tool_name == "Echo"
        return WorkflowNodeDispatchResult(str(arguments["value"]), True)

    def allowed_tool_names(self):
        return ("Echo",)


class State(GraphState):
    value: Annotated[int, Output] = 0


class LargeState(GraphState):
    value: Annotated[str, Output] = ""


async def increment(state: State) -> Stage:
    async def submit():
        return {"value": state.value + 1}

    return Stage(submit=submit())


def definition():
    builder = WorkflowBuilder("adapter", state_schema=State)
    builder.add_node("increment", increment)
    builder.add_edge(START, "increment")
    builder.add_edge("increment", END)
    return builder.build()


def durable_adapter(run, tmp_path, request_id="request"):
    durability = ProductWorkflowDurability(tmp_path / "workflows")
    projection = durability.control.create(
        CreateWorkflowRun(
            request_id,
            run.snapshot().definition_id,
            WorkflowRunCreationProvenance(
                WorkflowCreateAdmissionId(f"admission:{request_id}"),
                AgentId("agent"),
                IncarnationGeneration(1),
                LineageRevision(1),
                CancellationEpoch(0),
                SessionId("session"),
                AgentId("agent"),
                AbsoluteInstant(1, UNIX_UTC_CLOCK, 1),
            ),
            WorkflowRunAccessGrant(AgentId("agent"), AgentId("agent")),
            TrustedWorkflowBlueprintSource("test.workflow", 1),
            run.definition.digest,
            "{}",
        )
    )
    adapter = WorkflowExecutionAdapter(run, durability, projection, _Artifacts())
    adapter.bind_terminal_destination(f"test:{request_id}")
    return adapter, durability


async def execute_owned(adapter, durability):
    ownership = durability.claim_execution(adapter._projection.reference.run_id)
    try:
        return await adapter.execute(ownership)
    finally:
        durability.release_execution(ownership)


@pytest.mark.asyncio
async def test_adapter_maps_success_and_closes_idempotently(tmp_path):
    adapter, durability = durable_adapter(definition().start({"value": 1}), tmp_path)
    outcome = await execute_owned(adapter, durability)
    assert isinstance(outcome, Succeeded)
    assert outcome.output.value == 2
    await adapter.aclose()
    projection = durability.query(adapter._projection.reference)
    assert projection is not None
    assert projection.phase.value == "succeeded"
    restored_state, restored_run_state = definition().restore_checkpoint(projection.checkpoint_payload)
    assert restored_state.value == 2
    assert restored_run_state.completed_names() == {"increment"}
    await adapter.aclose()


@pytest.mark.asyncio
async def test_product_scan_delivers_durable_cancellation_to_active_execution(tmp_path):
    entered = asyncio.Event()

    async def block(state: State) -> Stage:
        async def submit():
            entered.set()
            await asyncio.Event().wait()
            return {"value": state.value + 1}

        return Stage(submit=submit())

    builder = WorkflowBuilder("cancel-active", state_schema=State)
    builder.add_node("block", block, implementation_id="test.block.v1")
    builder.add_edge(START, "block")
    builder.add_edge("block", END)
    adapter, durability = durable_adapter(builder.build().start({"value": 1}), tmp_path)
    await durability.start()
    durability.schedule_execution(adapter._projection.reference.run_id, adapter.execute, adapter.aclose)
    await asyncio.wait_for(entered.wait(), timeout=1)
    running = durability.query(adapter._projection.reference)
    assert running is not None and running.phase is WorkflowRunPhase.RUNNING
    durability.control.cancel(WorkflowRunCommand(running.reference, running.revision))
    for _ in range(100):
        current = durability.query(running.reference)
        if current is not None and current.phase is WorkflowRunPhase.CANCELLED:
            break
        await asyncio.sleep(0.01)
    else:
        raise AssertionError("active Workflow cancellation did not settle")
    await durability.aclose()


@pytest.mark.asyncio
async def test_product_execution_heartbeat_renews_same_fenced_epoch(tmp_path):
    durability = ProductWorkflowDurability(tmp_path / "heartbeat")
    durability._execution_lease_ttl_seconds = 0.06
    ownership = durability.claim_execution(WorkflowRunId("heartbeat-run"))
    stopped = asyncio.Event()

    async def execute(_ownership):
        await stopped.wait()

    async def cancel():
        stopped.set()

    durability.schedule_execution(WorkflowRunId("heartbeat-run"), execute, cancel, ownership=ownership)
    await asyncio.sleep(0.14)
    durability.assert_execution_current(WorkflowRunId("heartbeat-run"), ownership)
    stopped.set()
    await durability.aclose()


@pytest.mark.asyncio
async def test_process_restart_reactivates_trusted_blueprint_from_run_facts(tmp_path):
    root = tmp_path / "restart"
    workflow_definition = definition()
    source = TrustedWorkflowBlueprintSource("test.increment", 1)
    first = ProductWorkflowDurability(root, scan_interval_seconds=0.01)
    first.register_trusted_blueprint(source.blueprint_id, source.blueprint_version, lambda: workflow_definition)
    provenance = WorkflowRunCreationProvenance(
        WorkflowCreateAdmissionId("restart-admission"),
        AgentId("session"),
        IncarnationGeneration(1),
        LineageRevision(1),
        CancellationEpoch(0),
        SessionId("session"),
        AgentId("session"),
        AbsoluteInstant(1, UNIX_UTC_CLOCK, 1),
    )
    created = first.control.create(
        CreateWorkflowRun(
            "restart-request",
            workflow_definition.definition_id,
            provenance,
            WorkflowRunAccessGrant(AgentId("session"), AgentId("session")),
            source,
            workflow_definition.digest,
            '{"value":4}',
        )
    )
    await first.aclose()

    restarted = ProductWorkflowDurability(root, scan_interval_seconds=0.01)
    restarted.register_trusted_blueprint(source.blueprint_id, source.blueprint_version, lambda: workflow_definition)
    local = _LocalTasks(SessionId("session"))
    service = AgentWorkflowService(restarted, local, _Artifacts(), _WorkflowNodes())
    await restarted.start()
    for _ in range(100):
        current = restarted.query(created.reference)
        if current is not None and current.phase is WorkflowRunPhase.SUCCEEDED:
            break
        await asyncio.sleep(0.01)
    else:
        raise AssertionError("trusted Workflow was not reactivated after restart")
    assert current.terminal_result is not None
    await service.aclose()
    await restarted.aclose()


@pytest.mark.asyncio
async def test_process_restart_recompiles_declarative_spec_from_run_facts(tmp_path):
    root = tmp_path / "declarative-restart"
    nodes = _WorkflowNodes()
    spec = GraphSpec.model_validate(
        {
            "nodes": [{"id": "answer", "kind": "tool", "tool": "Echo", "args": {"value": {"$input": "value"}}}],
            "inputs": {"value": {"type": "integer"}},
            "output": {"$ref": "answer"},
        }
    )
    builder = build_graph(
        spec,
        dispatch=nodes.dispatch,
        command_name="RunGraph",
        valid_tools=set(nodes.allowed_tool_names()),
    )
    workflow_definition = builder.build()
    source = builder._definition_source
    assert source is not None
    first = ProductWorkflowDurability(root, scan_interval_seconds=0.01)
    provenance = WorkflowRunCreationProvenance(
        WorkflowCreateAdmissionId("declarative-admission"),
        AgentId("session"),
        IncarnationGeneration(1),
        LineageRevision(1),
        CancellationEpoch(0),
        SessionId("session"),
        AgentId("session"),
        AbsoluteInstant(1, UNIX_UTC_CLOCK, 1),
    )
    created = first.control.create(
        CreateWorkflowRun(
            "declarative-request",
            workflow_definition.definition_id,
            provenance,
            WorkflowRunAccessGrant(AgentId("session"), AgentId("session")),
            source,
            workflow_definition.digest,
            '{"value":4}',
        )
    )
    await first.aclose()

    restarted = ProductWorkflowDurability(root, scan_interval_seconds=0.01)
    service = AgentWorkflowService(restarted, _LocalTasks(SessionId("session")), _Artifacts(), nodes)
    await restarted.start()
    for _ in range(100):
        current = restarted.query(created.reference)
        if current is not None and current.phase is WorkflowRunPhase.SUCCEEDED:
            break
        await asyncio.sleep(0.01)
    else:
        raise AssertionError(f"declarative Workflow was not reactivated: {current!r}")
    await service.aclose()
    await restarted.aclose()


def test_declarative_graph_rejects_non_finite_literal() -> None:
    with pytest.raises(ValueError, match="non-finite numeric literals"):
        GraphSpec.model_validate(
            {
                "nodes": [
                    {
                        "id": "answer",
                        "kind": "tool",
                        "tool": "Echo",
                        "args": {"value": float("nan")},
                    }
                ],
                "output": {"$ref": "answer"},
            }
        )


def test_checkpoint_decoder_rejects_non_finite_json() -> None:
    with pytest.raises(ValueError, match="non-finite JSON"):
        definition().restore_checkpoint(
            '{"run_state":{"activity_execution_id":"run","records":{}},'
            '"schema":"mote.workflow-checkpoint/v2","state":{"value":NaN}}'
        )


@pytest.mark.asyncio
async def test_restart_resumes_checkpoint_without_rerunning_completed_node(tmp_path):
    executions = 0

    async def large(_state: LargeState) -> Stage:
        async def submit():
            nonlocal executions
            executions += 1
            return {"value": "x" * 70_000}

        return Stage(submit=submit())

    builder = WorkflowBuilder("restart-checkpoint", state_schema=LargeState)
    builder.trusted_blueprint("test.restart-checkpoint", 1)
    builder.add_node("large", large, implementation_id="test.restart-checkpoint.node/v1")
    builder.add_edge(START, "large")
    builder.add_edge("large", END)
    workflow_definition = builder.build()
    source = TrustedWorkflowBlueprintSource("test.restart-checkpoint", 1)
    root = tmp_path / "checkpoint-restart"
    first = ProductWorkflowDurability(root, scan_interval_seconds=0.01)
    first.register_trusted_blueprint(source.blueprint_id, source.blueprint_version, lambda: workflow_definition)
    provenance = WorkflowRunCreationProvenance(
        WorkflowCreateAdmissionId("checkpoint-admission"),
        AgentId("session"),
        IncarnationGeneration(1),
        LineageRevision(1),
        CancellationEpoch(0),
        SessionId("session"),
        AgentId("session"),
        AbsoluteInstant(1, UNIX_UTC_CLOCK, 1),
    )
    created = first.control.create(
        CreateWorkflowRun(
            "checkpoint-request",
            workflow_definition.definition_id,
            provenance,
            WorkflowRunAccessGrant(AgentId("session"), AgentId("session")),
            source,
            workflow_definition.digest,
            '{"value":""}',
        )
    )
    failing = _FailingArtifacts()
    interrupted_adapter = WorkflowExecutionAdapter(workflow_definition.start({"value": ""}), first, created, failing)
    interrupted_adapter.bind_terminal_destination("agent:session:workflow:checkpoint")
    with pytest.raises(OSError, match="artifact store unavailable"):
        await execute_owned(interrupted_adapter, first)
    interrupted = first.query(created.reference)
    assert interrupted is not None and interrupted.phase is WorkflowRunPhase.RUNNING
    assert interrupted.checkpoint_payload != "{}"
    assert executions == 1
    await interrupted_adapter.aclose()
    await first.aclose()

    restarted = ProductWorkflowDurability(root, scan_interval_seconds=0.01)
    restarted.register_trusted_blueprint(source.blueprint_id, source.blueprint_version, lambda: workflow_definition)
    service = AgentWorkflowService(restarted, _LocalTasks(SessionId("session")), _Artifacts(), _WorkflowNodes())
    await restarted.start()
    for _ in range(100):
        current = restarted.query(created.reference)
        if current is not None and current.phase is WorkflowRunPhase.SUCCEEDED:
            break
        await asyncio.sleep(0.01)
    else:
        raise AssertionError(f"checkpoint Workflow was not recovered: {current!r}")
    assert executions == 1
    await service.aclose()
    await restarted.aclose()


@pytest.mark.asyncio
async def test_restart_settles_cancelling_run_without_dispatching_nodes(tmp_path):
    executions = 0

    async def must_not_run(_state: State) -> Stage:
        async def submit():
            nonlocal executions
            executions += 1
            return {"value": 9}

        return Stage(submit=submit())

    builder = WorkflowBuilder("restart-cancelling", state_schema=State)
    builder.trusted_blueprint("test.restart-cancelling", 1)
    builder.add_node("node", must_not_run, implementation_id="test.cancel.node/v1")
    builder.add_edge(START, "node")
    builder.add_edge("node", END)
    workflow_definition = builder.build()
    source = TrustedWorkflowBlueprintSource("test.restart-cancelling", 1)
    root = tmp_path / "cancelling-restart"
    first = ProductWorkflowDurability(root, scan_interval_seconds=0.01)
    provenance = WorkflowRunCreationProvenance(
        WorkflowCreateAdmissionId("cancel-admission"),
        AgentId("session"),
        IncarnationGeneration(1),
        LineageRevision(1),
        CancellationEpoch(0),
        SessionId("session"),
        AgentId("session"),
        AbsoluteInstant(1, UNIX_UTC_CLOCK, 1),
    )
    created = first.control.create(
        CreateWorkflowRun(
            "cancel-request",
            workflow_definition.definition_id,
            provenance,
            WorkflowRunAccessGrant(AgentId("session"), AgentId("session")),
            source,
            workflow_definition.digest,
            '{"value":0}',
        )
    )
    cancelling = first.control.cancel(WorkflowRunCommand(created.reference, created.revision))
    await first.aclose()

    restarted = ProductWorkflowDurability(root, scan_interval_seconds=0.01)
    restarted.register_trusted_blueprint(source.blueprint_id, source.blueprint_version, lambda: workflow_definition)
    service = AgentWorkflowService(restarted, _LocalTasks(SessionId("session")), _Artifacts(), _WorkflowNodes())
    await restarted.start()
    for _ in range(100):
        current = restarted.query(cancelling.reference)
        if current is not None and current.phase is WorkflowRunPhase.CANCELLED:
            break
        await asyncio.sleep(0.01)
    else:
        raise AssertionError(f"cancelling Workflow was not settled: {current!r}")
    assert executions == 0
    await service.aclose()
    await restarted.aclose()


def test_unknown_or_changed_trusted_blueprint_fails_closed(tmp_path):
    durability = ProductWorkflowDurability(tmp_path / "blueprint-fail-closed")
    workflow_definition = definition()
    nodes = _WorkflowNodes()
    with pytest.raises(KeyError, match="not activated"):
        durability.resolve_definition_source(
            TrustedWorkflowBlueprintSource("missing", 1),
            expected_definition_id=workflow_definition.definition_id,
            expected_digest=workflow_definition.digest,
            workflow_nodes=nodes,
        )
    durability.register_trusted_blueprint("test.increment", 1, lambda: workflow_definition)
    with pytest.raises(ValueError, match="identity mismatch"):
        durability.resolve_definition_source(
            TrustedWorkflowBlueprintSource("test.increment", 1),
            expected_definition_id=workflow_definition.definition_id,
            expected_digest="0" * 64,
            workflow_nodes=nodes,
        )


@pytest.mark.asyncio
async def test_large_terminal_result_is_published_as_artifact(tmp_path):
    async def large(_state: LargeState) -> Stage:
        async def submit():
            return {"value": "x" * 70_000}

        return Stage(submit=submit())

    builder = WorkflowBuilder("large", state_schema=LargeState)
    builder.add_node("large", large, implementation_id="test.large/v1")
    builder.add_edge(START, "large")
    builder.add_edge("large", END)
    adapter, durability = durable_adapter(builder.build().start({"value": ""}), tmp_path)

    outcome = await execute_owned(adapter, durability)
    assert isinstance(outcome, Succeeded)
    projection = durability.query(adapter._projection.reference)
    assert projection is not None and projection.terminal_result is not None
    assert isinstance(projection.terminal_result.outcome, WorkflowSucceededArtifact)
    assert len(adapter._artifact_publisher.requests) == 1
    await adapter.aclose()


@pytest.mark.asyncio
async def test_large_terminal_publication_recovers_from_checkpoint_without_rerun(
    tmp_path,
):
    executions = 0

    async def large(_state: LargeState) -> Stage:
        async def submit():
            nonlocal executions
            executions += 1
            return {"value": "x" * 70_000}

        return Stage(submit=submit())

    builder = WorkflowBuilder("large-recovery", state_schema=LargeState)
    builder.add_node("large", large, implementation_id="test.large-recovery/v1")
    builder.add_edge(START, "large")
    builder.add_edge("large", END)
    workflow_definition = builder.build()
    run = workflow_definition.start({"value": ""})
    adapter, durability = durable_adapter(run, tmp_path, "publication-recovery")
    failing = _FailingArtifacts()
    adapter._artifact_publisher = failing

    with pytest.raises(OSError, match="artifact store unavailable"):
        await execute_owned(adapter, durability)
    interrupted = durability.query(adapter._projection.reference)
    assert interrupted is not None
    assert interrupted.phase.value == "running"
    assert interrupted.checkpoint_payload != "{}"
    assert executions == 1
    publication_id = failing.requests[0][0]

    checkpoint, run_state = workflow_definition.restore_checkpoint(interrupted.checkpoint_payload)
    restored_run = workflow_definition.start(
        {},
        checkpoint=checkpoint,
        run_state=run_state,
        from_nodes=interrupted.frontier,
        skip_nodes=tuple(run_state.completed_names()),
    )
    recovered_artifacts = _Artifacts()
    recovered = WorkflowExecutionAdapter(restored_run, durability, interrupted, recovered_artifacts)
    recovered.bind_terminal_destination("test:publication-recovery")
    outcome = await execute_owned(recovered, durability)
    assert isinstance(outcome, Succeeded)
    assert executions == 1
    assert recovered_artifacts.requests[0][0] == publication_id
    terminal = durability.query(interrupted.reference)
    assert terminal is not None
    assert terminal.phase.value == "succeeded"
    assert isinstance(terminal.terminal_result.outcome, WorkflowSucceededArtifact)
    await adapter.aclose()
    await recovered.aclose()


@pytest.mark.asyncio
async def test_workflow_pause_requires_durable_run_resume_path(tmp_path):
    builder = WorkflowBuilder("pause", state_schema=State)
    builder.add_node("increment", increment)
    builder.add_node("again", increment)
    builder.add_edge(START, "increment")
    builder.add_llm_edges("increment", "continue?", {"yes": "again", "no": END})
    adapter, durability = durable_adapter(builder.build().start({"value": 1}), tmp_path)
    outcome = await execute_owned(adapter, durability)
    assert isinstance(outcome, Paused)
    projection = durability.query(adapter._projection.reference)
    assert projection is not None and projection.phase.value == "paused"


def test_stale_execution_owner_cannot_emit_workflow_progress(tmp_path):
    adapter, durability = durable_adapter(definition().start({"value": 1}), tmp_path)
    ownership = durability.claim_execution(adapter._projection.reference.run_id)
    adapter._execution_ownership = ownership
    durability.release_execution(ownership)
    with pytest.raises(LeaseFencedError):
        adapter.emit(
            ActivityProgressEvent(
                ActivityProgressIdentity(
                    "activity",
                    str(adapter._projection.reference.definition_id),
                ),
                "increment",
                ProgressPhase.RUNNING,
            )
        )
