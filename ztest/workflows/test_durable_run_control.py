from __future__ import annotations

import json
import threading

import pytest

from mote.contracts.agent.runtime_identity import AgentId, CancellationEpoch, IncarnationGeneration, LineageRevision
from mote.contracts.clock import UNIX_UTC_CLOCK, AbsoluteInstant
from mote.contracts.runtime.operation_ownership import OperationBackend
from mote.contracts.session.identity import SessionId
from mote.contracts.workflow import (
    TrustedWorkflowBlueprintSource,
    WorkflowCancelled,
    WorkflowCreateAdmissionId,
    WorkflowDefinitionId,
    WorkflowRunAccessGrant,
    WorkflowRunCreationProvenance,
    WorkflowSucceededInline,
    WorkflowTerminalResult,
)
from mote.orchestration.workflows.durable import (
    CreateWorkflowRun,
    PauseWorkflowRun,
    ResumeWorkflowRun,
    SettleWorkflowRun,
    WorkflowPauseReason,
    WorkflowRunCommand,
    WorkflowRunControl,
    WorkflowRunPhase,
    WorkflowRunStore,
)
from mote.runtime.control.leases import InMemoryLeaseCoordinator
from mote.runtime.control.operation_ownership import LeaseOperationOwnership


def _control(tmp_path):
    ownership = LeaseOperationOwnership(InMemoryLeaseCoordinator(), backend=OperationBackend.LOCAL_FILE)
    store = WorkflowRunStore(tmp_path / "runs.json", ownership)
    return (
        WorkflowRunControl(
            store, ownership, deployment_id="test", holder_id="worker", backend=OperationBackend.LOCAL_FILE
        ),
        store,
    )


def _create(request: str = "request") -> CreateWorkflowRun:
    agent = AgentId("agent")
    return CreateWorkflowRun(
        request,
        WorkflowDefinitionId("definition"),
        WorkflowRunCreationProvenance(
            WorkflowCreateAdmissionId(f"admission:{request}"),
            agent,
            IncarnationGeneration(1),
            LineageRevision(1),
            CancellationEpoch(0),
            SessionId("session"),
            agent,
            AbsoluteInstant(1, UNIX_UTC_CLOCK, 1),
        ),
        WorkflowRunAccessGrant(agent, agent),
        TrustedWorkflowBlueprintSource("test.workflow", 1),
        "0" * 64,
        "{}",
    )


def test_create_rejects_non_finite_initial_input_json() -> None:
    baseline = _create()
    with pytest.raises(ValueError, match="non-finite JSON"):
        CreateWorkflowRun(
            baseline.request_id,
            baseline.definition_id,
            baseline.provenance,
            baseline.access_grant,
            baseline.definition_source,
            baseline.definition_digest,
            '{"value":NaN}',
        )


def test_create_is_durable_idempotent_and_request_identity_is_stable(tmp_path) -> None:
    control, store = _control(tmp_path)
    baseline = _create()
    command = CreateWorkflowRun(
        baseline.request_id,
        baseline.definition_id,
        baseline.provenance,
        baseline.access_grant,
        baseline.definition_source,
        baseline.definition_digest,
        baseline.initial_input_payload,
        checkpoint_payload='{"input":1}',
        frontier=("start",),
    )
    first = control.create(command)
    assert control.create(command) == first
    assert WorkflowRunStore(store._path, store._ownership).get(first.reference) == first
    assert _create().reference == first.reference


def test_pause_resume_token_binds_run_revision(tmp_path) -> None:
    control, _ = _control(tmp_path)
    created = control.create(_create())
    running = control.start(WorkflowRunCommand(created.reference, created.revision))
    paused = control.pause(
        PauseWorkflowRun(
            running.reference, running.revision, WorkflowPauseReason.EXTERNAL_INPUT, '{"checkpoint":1}', ("node-b",)
        )
    )
    with pytest.raises(RuntimeError, match="resume token"):
        control.resume(
            ResumeWorkflowRun(
                paused.reference,
                paused.revision,
                "wrong",
                paused.checkpoint_payload,
                paused.frontier,
            )
        )
    resumed = control.resume(
        ResumeWorkflowRun(
            paused.reference,
            paused.revision,
            paused.resume_nonce,
            paused.checkpoint_payload,
            paused.frontier,
        )
    )
    assert resumed.phase is WorkflowRunPhase.RUNNING
    assert resumed.frontier == ("node-b",)


def test_cancel_and_terminal_race_has_one_cas_winner(tmp_path) -> None:
    control, _ = _control(tmp_path)
    created = control.create(_create())
    running = control.start(WorkflowRunCommand(created.reference, created.revision))
    cancelling = control.cancel(WorkflowRunCommand(running.reference, running.revision))
    with pytest.raises(RuntimeError, match="canonical state"):
        control.settle(
            SettleWorkflowRun(
                running.reference,
                running.revision,
                WorkflowRunPhase.SUCCEEDED,
                WorkflowTerminalResult(running.reference.run_id, running.revision + 1, WorkflowSucceededInline("done")),
            )
        )
    cancelled = control.settle(
        SettleWorkflowRun(
            cancelling.reference,
            cancelling.revision,
            WorkflowRunPhase.CANCELLED,
            WorkflowTerminalResult(cancelling.reference.run_id, cancelling.revision + 1, WorkflowCancelled("operator")),
        )
    )
    assert cancelled.phase.terminal


def test_store_strict_decoder_rejects_extra_fields(tmp_path) -> None:
    control, store = _control(tmp_path)
    control.create(_create())
    payload = json.loads(store._path.read_text(encoding="utf-8"))
    payload["runs"][0]["extra"] = True
    store._path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="shape"):
        store.scan()


@pytest.mark.parametrize("missing", ["provenance", "access_grant"])
def test_old_run_without_authority_facts_fails_closed(tmp_path, missing) -> None:
    control, store = _control(tmp_path)
    control.create(_create())
    payload = json.loads(store._path.read_text(encoding="utf-8"))
    del payload["runs"][0][missing]
    store._path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="shape"):
        store.scan()


def test_old_workflow_store_schema_has_no_fallback_decoder(tmp_path) -> None:
    control, store = _control(tmp_path)
    control.create(_create())
    payload = json.loads(store._path.read_text(encoding="utf-8"))
    payload["schema"] = "mote.workflow-run-store/v1"
    store._path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="envelope"):
        store.scan()


def test_distinct_run_commits_share_one_cross_process_store_transaction(tmp_path) -> None:
    first, first_store = _control(tmp_path)
    second, _ = _control(tmp_path)
    barrier = threading.Barrier(2)
    failures: list[BaseException] = []

    def create(control: WorkflowRunControl, request_id: str) -> None:
        try:
            barrier.wait()
            control.create(_create(request_id))
        except BaseException as exc:
            failures.append(exc)

    threads = (
        threading.Thread(target=create, args=(first, "request-a")),
        threading.Thread(target=create, args=(second, "request-b")),
    )
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=2)
    assert failures == []
    assert {item.request_id for item in first_store.scan()} == {"request-a", "request-b"}
