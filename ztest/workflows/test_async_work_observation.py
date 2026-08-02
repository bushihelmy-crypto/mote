import pytest

from mote.contracts.agent.runtime_identity import AgentId, CancellationEpoch, IncarnationGeneration, LineageRevision
from mote.contracts.async_work.command import (
    CancelDurableWorkflowRun,
    ResumeDurableWorkflowRun,
    WorkflowCancelDisposition,
    WorkflowResumeDisposition,
)
from mote.contracts.async_work.identity import DurableWorkflowRunReference
from mote.contracts.clock import UNIX_UTC_CLOCK, AbsoluteInstant
from mote.contracts.ports.async_work import AsyncWorkQueryDisposition
from mote.contracts.runtime.operation_ownership import (
    EffectCapability,
    OperationBackend,
    OperationOwnership,
    OperationOwnershipRequest,
)
from mote.contracts.session.identity import SessionId
from mote.contracts.workflow import (
    TrustedWorkflowBlueprintSource,
    WorkflowCallerAuthorizationDisposition,
    WorkflowCallerAuthorizationReceipt,
    WorkflowCallerContext,
    WorkflowCreateAdmission,
    WorkflowCreateAdmissionId,
    WorkflowCreateAdmissionLifecycle,
    WorkflowDefinitionId,
    WorkflowRunAccessGrant,
    WorkflowRunCreationProvenance,
)
from mote.contracts.workflow.command import WorkflowCancelReason
from mote.orchestration.workflows.durable import (
    CreateWorkflowRun,
    PauseWorkflowRun,
    WorkflowPauseReason,
    WorkflowReconciliationStore,
    WorkflowRunCommand,
    WorkflowRunControl,
    WorkflowRunPhase,
    WorkflowRunStore,
)
from mote.orchestration.workflows.observation import BoundWorkflowAsyncWorkAdapter
from mote.runtime.control.leases import InMemoryLeaseCoordinator
from mote.runtime.control.operation_ownership import LeaseOperationOwnership


class _Authorizer:
    def __init__(self, disposition=WorkflowCallerAuthorizationDisposition.AUTHORIZED):
        self.disposition = disposition

    def authorize_workflow_caller(self, caller):
        return WorkflowCallerAuthorizationReceipt(self.disposition, caller.lineage_revision)


class _Admissions:
    def __init__(self, admission):
        self.admission = admission

    def get_workflow_create_admission(self, admission_id):
        if self.admission is not None and self.admission.admission_id == admission_id:
            return self.admission
        return None


def _adapter(tmp_path, *, caller_agent="agent"):
    ownership = LeaseOperationOwnership(InMemoryLeaseCoordinator(), backend=OperationBackend.LOCAL_FILE)
    runs = WorkflowRunStore(tmp_path / "runs.json", ownership)
    control = WorkflowRunControl(
        runs,
        ownership,
        deployment_id="test",
        holder_id="owner",
        backend=OperationBackend.LOCAL_FILE,
    )
    now = AbsoluteInstant(1, UNIX_UTC_CLOCK, 1)
    provenance = WorkflowRunCreationProvenance(
        WorkflowCreateAdmissionId("admission"),
        AgentId("agent"),
        IncarnationGeneration(1),
        LineageRevision(2),
        CancellationEpoch(0),
        SessionId("session"),
        AgentId("root"),
        now,
    )
    run = control.create(
        CreateWorkflowRun(
            "create",
            WorkflowDefinitionId("definition"),
            provenance,
            WorkflowRunAccessGrant(AgentId("agent"), AgentId("root")),
            TrustedWorkflowBlueprintSource("test.workflow", 1),
            "0" * 64,
            "{}",
        )
    )
    admission = WorkflowCreateAdmission(
        provenance.workflow_create_admission_id,
        "create",
        run.reference,
        provenance.creator_logical_agent_id,
        provenance.root_governance_agent_id,
        provenance.creator_lineage_revision,
        provenance.creator_cancellation_epoch,
        1,
        OperationOwnership(
            OperationOwnershipRequest(
                "test",
                str(provenance.workflow_create_admission_id),
                "creator",
                OperationBackend.LOCAL_FILE,
                0,
                f"workflow-create:{provenance.workflow_create_admission_id}",
                EffectCapability.NO_EXTERNAL_EFFECT,
            ),
            "creator",
            1,
            30.0,
        ),
        WorkflowCreateAdmissionLifecycle.COMMITTED,
    )
    caller = WorkflowCallerContext(
        AgentId(caller_agent),
        AgentId("root"),
        IncarnationGeneration(1),
        LineageRevision(2),
        CancellationEpoch(0),
        1,
    )
    adapter = BoundWorkflowAsyncWorkAdapter(
        caller,
        _Authorizer(),
        _Admissions(admission),
        runs,
        WorkflowReconciliationStore(tmp_path / "reconcile.json", ownership),
        control,
    )
    return adapter, runs, run


def test_bound_workflow_observation_and_cancel_preserve_cas(tmp_path) -> None:
    adapter, runs, run = _adapter(tmp_path)
    reference = DurableWorkflowRunReference(run.reference)
    query = adapter.get(reference)
    assert query.disposition is AsyncWorkQueryDisposition.FOUND
    receipt = adapter.cancel(CancelDurableWorkflowRun(reference, run.revision, WorkflowCancelReason.AGENT_REQUEST))
    assert receipt.disposition is WorkflowCancelDisposition.CANCEL_REQUESTED
    current = runs.get(run.reference)
    assert current is not None and current.phase is WorkflowRunPhase.CANCELLING
    assert (
        adapter.cancel(
            CancelDurableWorkflowRun(reference, run.revision, WorkflowCancelReason.AGENT_REQUEST)
        ).disposition
        is WorkflowCancelDisposition.REVISION_CONFLICT
    )


def test_bound_workflow_observation_rejects_foreign_principal(tmp_path) -> None:
    adapter, _, run = _adapter(tmp_path, caller_agent="foreign")
    reference = DurableWorkflowRunReference(run.reference)
    assert adapter.get(reference).disposition is AsyncWorkQueryDisposition.PRINCIPAL_MISMATCH
    assert (
        adapter.cancel(
            CancelDurableWorkflowRun(reference, run.revision, WorkflowCancelReason.AGENT_REQUEST)
        ).disposition
        is WorkflowCancelDisposition.PRINCIPAL_MISMATCH
    )


def test_bound_workflow_resume_preserves_authority_nonce_and_cas(tmp_path) -> None:
    adapter, runs, run = _adapter(tmp_path)
    running = adapter._control.start(WorkflowRunCommand(run.reference, run.revision))
    paused = adapter._control.pause(
        PauseWorkflowRun(
            running.reference,
            running.revision,
            WorkflowPauseReason.OPERATOR,
            "{}",
            ("next",),
        )
    )
    reference = DurableWorkflowRunReference(run.reference)
    receipt = adapter.resume(
        ResumeDurableWorkflowRun(
            reference,
            paused.revision,
            paused.resume_nonce,
            paused.checkpoint_payload,
            paused.frontier,
        )
    )
    assert receipt.disposition is WorkflowResumeDisposition.RESUMED
    current = runs.get(run.reference)
    assert current is not None and current.phase is WorkflowRunPhase.RUNNING
    assert (
        adapter.resume(
            ResumeDurableWorkflowRun(
                reference,
                paused.revision,
                paused.resume_nonce,
                paused.checkpoint_payload,
                paused.frontier,
            )
        ).disposition
        is WorkflowResumeDisposition.REVISION_CONFLICT
    )


@pytest.mark.parametrize(
    ("authority", "query", "cancel", "resume"),
    [
        (
            WorkflowCallerAuthorizationDisposition.NOT_ACTIVE,
            AsyncWorkQueryDisposition.CONTROL_UNAVAILABLE,
            WorkflowCancelDisposition.CALLER_NOT_ACTIVE,
            WorkflowResumeDisposition.CALLER_NOT_ACTIVE,
        ),
        (
            WorkflowCallerAuthorizationDisposition.INCARNATION_MISMATCH,
            AsyncWorkQueryDisposition.CONTROL_UNAVAILABLE,
            WorkflowCancelDisposition.INCARNATION_MISMATCH,
            WorkflowResumeDisposition.INCARNATION_MISMATCH,
        ),
        (
            WorkflowCallerAuthorizationDisposition.ROOT_MISMATCH,
            AsyncWorkQueryDisposition.PRINCIPAL_MISMATCH,
            WorkflowCancelDisposition.PRINCIPAL_MISMATCH,
            WorkflowResumeDisposition.PRINCIPAL_MISMATCH,
        ),
        (
            WorkflowCallerAuthorizationDisposition.STALE_FENCE,
            AsyncWorkQueryDisposition.CONTROL_UNAVAILABLE,
            WorkflowCancelDisposition.FENCE_LOST,
            WorkflowResumeDisposition.FENCE_LOST,
        ),
    ],
)
def test_bound_workflow_authority_failures_do_not_mutate_run(tmp_path, authority, query, cancel, resume) -> None:
    adapter, runs, run = _adapter(tmp_path)
    adapter._authorizer.disposition = authority
    reference = DurableWorkflowRunReference(run.reference)

    assert adapter.get(reference).disposition is query
    assert (
        adapter.cancel(
            CancelDurableWorkflowRun(reference, run.revision, WorkflowCancelReason.AGENT_REQUEST)
        ).disposition
        is cancel
    )
    assert (
        adapter.resume(
            ResumeDurableWorkflowRun(
                reference,
                run.revision,
                "nonce",
                run.checkpoint_payload,
                run.frontier,
            )
        ).disposition
        is resume
    )
    assert runs.get(run.reference) == run


def test_session_provenance_is_not_workflow_access_authority() -> None:
    assert "creator_session_id" in WorkflowRunCreationProvenance.__dataclass_fields__
    assert "session_id" not in WorkflowCallerContext.__dataclass_fields__
    assert "session_id" not in WorkflowRunAccessGrant.__dataclass_fields__


def test_run_without_committed_admission_mapping_fails_closed(tmp_path) -> None:
    adapter, runs, run = _adapter(tmp_path)
    adapter._admissions.admission = None
    reference = DurableWorkflowRunReference(run.reference)

    assert adapter.get(reference).disposition is AsyncWorkQueryDisposition.CONTROL_UNAVAILABLE
    assert (
        adapter.cancel(
            CancelDurableWorkflowRun(reference, run.revision, WorkflowCancelReason.AGENT_REQUEST)
        ).disposition
        is WorkflowCancelDisposition.CONTROL_UNAVAILABLE
    )
    assert runs.get(run.reference) == run
