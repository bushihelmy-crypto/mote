from __future__ import annotations

from mote.contracts.runtime.operation_ownership import EffectCapability, OperationBackend, OperationOwnershipRequest
from mote.contracts.workflow import WorkflowCreateAdmissionDisposition, WorkflowCreateAdmissionLifecycle
from mote.orchestration.agents.lineage.store import AgentLineageStore
from mote.orchestration.workflows.creation import WorkflowCreateAdmissionReconciler
from mote.orchestration.workflows.durable import WorkflowRunControl, WorkflowRunStore
from mote.runtime.control.leases import InMemoryLeaseCoordinator
from mote.runtime.control.operation_ownership import LeaseOperationOwnership
from ztest.workflows.test_durable_run_control import _create


class _Admissions:
    def __init__(self, store: AgentLineageStore, lease) -> None:
        self._store = store
        self._lease = lease

    def reserved_workflow_create_admissions(self):
        return tuple(
            item
            for item in self._store.workflow_create_admissions()
            if item.lifecycle is WorkflowCreateAdmissionLifecycle.RESERVED
        )

    def get_workflow_create_admission(self, admission_id):
        return self._store.get_workflow_create_admission(admission_id)

    def claim_workflow_create_admission(self, command):
        return self._store.claim_workflow_create(
            command.admission_id,
            expected_revision=command.expected_revision,
            ownership=command.ownership,
            lease=self._lease,
        )

    def settle_workflow_create_admission(self, command):
        return self._store.settle_workflow_create(
            command.admission_id,
            command.lifecycle,
            expected_revision=command.expected_revision,
            ownership=command.ownership,
            lease=self._lease,
        )

    def reserve_workflow_create_admission(self, command):
        raise AssertionError("recovery never creates an admission")


def _scenario(tmp_path):
    now = [0.0]
    operation_leases = InMemoryLeaseCoordinator(clock=lambda: now[0])
    ownership = LeaseOperationOwnership(operation_leases, backend=OperationBackend.LOCAL_FILE)
    lineage_leases = InMemoryLeaseCoordinator()
    lineage_lease = lineage_leases.acquire("lineage", "supervisor", 30)
    lineage = AgentLineageStore(tmp_path / "lineage.json", lease_coordinator=lineage_leases)
    root = lineage.register_root("agent", "agent-definition", lease=lineage_lease)
    command = _create()
    admission_id = command.provenance.workflow_create_admission_id
    original = ownership.claim(
        OperationOwnershipRequest(
            "test",
            str(admission_id),
            "creator",
            OperationBackend.LOCAL_FILE,
            0,
            f"workflow-create:{admission_id}",
            EffectCapability.NO_EXTERNAL_EFFECT,
        ),
        10,
    )
    reserved = lineage.reserve_workflow_create(
        admission_id=admission_id,
        create_request_id=command.request_id,
        reference=command.reference,
        logical_agent_id="agent",
        expected_lineage_revision=root.revision,
        cancellation_epoch=0,
        ownership=original,
        lease=lineage_lease,
    )
    assert reserved.admission is not None
    run_store = WorkflowRunStore(tmp_path / "runs.json", ownership)
    control = WorkflowRunControl(
        run_store,
        ownership,
        deployment_id="test",
        holder_id="run-owner",
        backend=OperationBackend.LOCAL_FILE,
    )
    reconciler = WorkflowCreateAdmissionReconciler(
        _Admissions(lineage, lineage_lease),
        run_store,
        ownership,
        holder_id="recovery",
        lease_ttl_seconds=10,
    )
    return now, original, command, control, lineage, reconciler


def test_live_create_owner_prevents_recovery_claim(tmp_path) -> None:
    _, _, _, _, lineage, reconciler = _scenario(tmp_path)
    assert reconciler.reconcile() == 0
    assert lineage.workflow_create_admissions()[0].lifecycle is WorkflowCreateAdmissionLifecycle.RESERVED


def test_recovery_commits_admission_when_run_commit_survived(tmp_path) -> None:
    now, ownership, command, control, lineage, reconciler = _scenario(tmp_path)
    control.create(command, admission_ownership=ownership)
    now[0] = 11
    assert reconciler.reconcile() == 1
    assert lineage.workflow_create_admissions()[0].lifecycle is WorkflowCreateAdmissionLifecycle.COMMITTED


def test_recovery_aborts_without_replaying_missing_run(tmp_path) -> None:
    now, _, command, control, lineage, reconciler = _scenario(tmp_path)
    now[0] = 11
    assert reconciler.reconcile() == 1
    assert control._store.get(command.reference) is None
    assert lineage.workflow_create_admissions()[0].lifecycle is WorkflowCreateAdmissionLifecycle.ABORTED
    aborted = lineage.workflow_create_admissions()[0]
    retry = lineage.reserve_workflow_create(
        admission_id=aborted.admission_id,
        create_request_id=aborted.create_request_id,
        reference=aborted.reference,
        logical_agent_id=str(aborted.logical_agent_id),
        expected_lineage_revision=int(aborted.lineage_revision),
        cancellation_epoch=int(aborted.cancellation_epoch),
        ownership=aborted.ownership,
        lease=reconciler._admissions._lease,
    )
    assert retry.disposition is WorkflowCreateAdmissionDisposition.PREVIOUS_ADMISSION_ABORTED
    assert retry.admission == aborted
