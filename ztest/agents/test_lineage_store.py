from __future__ import annotations

from dataclasses import replace

import pytest

from mote.contracts.agent.capacity import CapacityReservationDisposition, LogicalCapacityReservationReceipt
from mote.contracts.agent.lineage import (
    LineageAuthorizationDisposition,
    SpawnAdvanceDisposition,
    SpawnLifecycle,
    SpawnRequest,
)
from mote.contracts.agent.runtime_identity import AgentId, CancellationEpoch, LineageRevision
from mote.contracts.runtime.operation_ownership import (
    EffectCapability,
    OperationBackend,
    OperationOwnership,
    OperationOwnershipRequest,
)
from mote.contracts.workflow import (
    WorkflowCreateAdmissionDisposition,
    WorkflowCreateAdmissionId,
    WorkflowDefinitionId,
    WorkflowGovernanceCancelRequest,
    WorkflowGovernanceSnapshotVerification,
    WorkflowRunId,
    WorkflowRunReference,
)
from mote.orchestration.agents.lineage.codec import decode_lineage, encode_lineage
from mote.orchestration.agents.lineage.store import AgentLineageStore
from mote.orchestration.agents.lineage.workflow_governance import AgentLineageWorkflowGovernanceVerifier
from mote.runtime.control.leases import InMemoryLeaseCoordinator


def _capacity(identity: str) -> LogicalCapacityReservationReceipt:
    return LogicalCapacityReservationReceipt(identity, 1, (), CapacityReservationDisposition.RESERVED)


def _ownership(admission_id: str, lease) -> OperationOwnership:
    return OperationOwnership(
        OperationOwnershipRequest(
            "test",
            admission_id,
            "supervisor-1",
            OperationBackend.LOCAL_FILE,
            0,
            f"workflow-create:{admission_id}",
            EffectCapability.NO_EXTERNAL_EFFECT,
        ),
        lease.subject,
        lease.fencing_token,
        lease.expires_at,
    )


def _advance(store, record, target, lease, **kwargs):
    receipt = store.advance(record.request.request_id, target, expected_revision=record.revision, lease=lease, **kwargs)
    assert receipt.disposition is SpawnAdvanceDisposition.APPLIED
    assert receipt.record is not None
    return receipt.record


def _activate(store, request, lease):
    receipt = store.request_spawn(
        request, capacity=_capacity(request.capacity_reservation_id), budget=None, lease=lease
    )
    assert receipt.record is not None
    record = _advance(store, receipt.record, SpawnLifecycle.ADMITTED, lease)
    record = _advance(store, record, SpawnLifecycle.LINEAGE_COMMITTED, lease)
    record = _advance(store, record, SpawnLifecycle.PLACEMENT_PENDING, lease, placement="worker-1")
    record = _advance(
        store, record, SpawnLifecycle.INCARNATION_STARTED, lease, placement="worker-1", incarnation_generation=1
    )
    return _advance(store, record, SpawnLifecycle.ACTIVE, lease)


def test_cold_start_rebuilds_ten_generation_tree_and_subtree_index(tmp_path):
    coordinator = InMemoryLeaseCoordinator()
    lease = coordinator.acquire("lineage", "supervisor-1", 30)
    path = tmp_path / "lineage.json"
    store = AgentLineageStore(path, lease_coordinator=coordinator)
    parent = store.register_root("root", "root-definition", lease=lease)
    parent_id = parent.logical_agent_id
    assert parent_id is not None
    parent_path = "/root"
    created = []
    for depth in range(1, 11):
        parent_path = f"{parent_path}/n{depth}"
        request = SpawnRequest(
            f"request-{depth}",
            "root",
            parent_id,
            parent_path,
            f"n{depth}",
            f"definition-{depth}",
            f"capacity-{depth}",
            (),
        )
        record = _activate(store, request, lease)
        assert record.logical_agent_id is not None
        parent_id = record.logical_agent_id
        created.append(parent_id)

    restarted = AgentLineageStore(path, lease_coordinator=coordinator)
    snapshot = restarted.subtree_snapshot("root")
    assert set(snapshot.agent_ids) == {"root", *created}
    leaf = restarted.subtree_snapshot(created[-1])
    assert leaf.agent_ids == (created[-1],)


def test_workflow_admission_and_cancellation_snapshot_share_lineage_transaction(tmp_path):
    coordinator = InMemoryLeaseCoordinator()
    lease = coordinator.acquire("lineage", "supervisor-1", 30)
    store = AgentLineageStore(tmp_path / "lineage.json", lease_coordinator=coordinator)
    root = store.register_root("root", "root-definition", lease=lease)
    reference = WorkflowRunReference(WorkflowRunId("wfr_1"), WorkflowDefinitionId("definition"))
    receipt = store.reserve_workflow_create(
        admission_id=WorkflowCreateAdmissionId("admission-1"),
        create_request_id="create-1",
        reference=reference,
        logical_agent_id="root",
        expected_lineage_revision=root.revision,
        cancellation_epoch=0,
        ownership=_ownership("admission-1", lease),
        lease=lease,
    )
    assert receipt.disposition is WorkflowCreateAdmissionDisposition.RESERVED
    snapshot = store.begin_subtree_cancellation("root", lease=lease)
    assert snapshot.workflow_create_admission_ids == ("admission-1",)
    rejected = store.reserve_workflow_create(
        admission_id=WorkflowCreateAdmissionId("admission-2"),
        create_request_id="create-2",
        reference=WorkflowRunReference(WorkflowRunId("wfr_2"), WorkflowDefinitionId("definition")),
        logical_agent_id="root",
        expected_lineage_revision=snapshot.revision,
        cancellation_epoch=0,
        ownership=_ownership("admission-2", lease),
        lease=lease,
    )
    assert rejected.disposition is WorkflowCreateAdmissionDisposition.STALE_REVISION
    request = WorkflowGovernanceCancelRequest(
        WorkflowGovernanceCancelRequest.derive_id(
            AgentId(snapshot.root_agent_id),
            AgentId(snapshot.subtree_agent_id),
            CancellationEpoch(snapshot.cancellation_epoch),
        ),
        AgentId(snapshot.root_agent_id),
        AgentId(snapshot.subtree_agent_id),
        LineageRevision(snapshot.revision),
        CancellationEpoch(snapshot.cancellation_epoch),
        tuple(AgentId(value) for value in snapshot.agent_ids),
        tuple(WorkflowCreateAdmissionId(value) for value in snapshot.workflow_create_admission_ids),
    )
    verifier = AgentLineageWorkflowGovernanceVerifier(store)
    assert verifier.verify(request) is WorkflowGovernanceSnapshotVerification.VERIFIED
    assert (
        verifier.verify(
            replace(
                request,
                target_agent_ids=(request.subtree_agent_id, AgentId("unknown")),
            )
        )
        is WorkflowGovernanceSnapshotVerification.SCOPE_MISMATCH
    )
    assert (
        verifier.verify(replace(request, admitted_workflow_create_ids=()))
        is WorkflowGovernanceSnapshotVerification.SCOPE_MISMATCH
    )
    wrong_root = AgentId("other-root")
    assert (
        verifier.verify(
            WorkflowGovernanceCancelRequest(
                WorkflowGovernanceCancelRequest.derive_id(
                    wrong_root,
                    request.subtree_agent_id,
                    request.cancellation_epoch,
                ),
                wrong_root,
                request.subtree_agent_id,
                request.lineage_snapshot_revision,
                request.cancellation_epoch,
                request.target_agent_ids,
                request.admitted_workflow_create_ids,
            )
        )
        is WorkflowGovernanceSnapshotVerification.SCOPE_MISMATCH
    )


@pytest.mark.parametrize(
    "phase",
    [
        SpawnLifecycle.REQUESTED,
        SpawnLifecycle.ADMITTED,
        SpawnLifecycle.LINEAGE_COMMITTED,
        SpawnLifecycle.PLACEMENT_PENDING,
        SpawnLifecycle.INCARNATION_STARTED,
    ],
)
def test_each_spawn_intermediate_state_is_durably_reconcilable(tmp_path, phase):
    coordinator = InMemoryLeaseCoordinator()
    lease = coordinator.acquire("lineage", "supervisor-1", 30)
    store = AgentLineageStore(tmp_path / "lineage.json", lease_coordinator=coordinator)
    store.register_root("root", "root-definition", lease=lease)
    request = SpawnRequest("request", "root", "root", "/root/child", "child", "definition", "capacity", ())
    record = store.request_spawn(request, capacity=_capacity("capacity"), budget=None, lease=lease).record
    assert record is not None
    if phase in {
        SpawnLifecycle.ADMITTED,
        SpawnLifecycle.LINEAGE_COMMITTED,
        SpawnLifecycle.PLACEMENT_PENDING,
        SpawnLifecycle.INCARNATION_STARTED,
    }:
        record = _advance(store, record, SpawnLifecycle.ADMITTED, lease)
    if phase in {
        SpawnLifecycle.LINEAGE_COMMITTED,
        SpawnLifecycle.PLACEMENT_PENDING,
        SpawnLifecycle.INCARNATION_STARTED,
    }:
        record = _advance(store, record, SpawnLifecycle.LINEAGE_COMMITTED, lease)
    if phase in {SpawnLifecycle.PLACEMENT_PENDING, SpawnLifecycle.INCARNATION_STARTED}:
        record = _advance(store, record, SpawnLifecycle.PLACEMENT_PENDING, lease, placement="worker")
    if phase is SpawnLifecycle.INCARNATION_STARTED:
        _advance(store, record, phase, lease, placement="worker", incarnation_generation=1)
    restarted = AgentLineageStore(tmp_path / "lineage.json", lease_coordinator=coordinator)
    assert restarted.pending_reconciliation()[0].lifecycle is phase


def test_duplicate_request_is_idempotent_but_changed_definition_conflicts(tmp_path):
    coordinator = InMemoryLeaseCoordinator()
    lease = coordinator.acquire("lineage", "owner", 30)
    store = AgentLineageStore(tmp_path / "lineage.json", lease_coordinator=coordinator)
    store.register_root("root", "root-definition", lease=lease)
    request = SpawnRequest("request", "root", "root", "/root/a", "a", "definition", "capacity", ())
    first = store.request_spawn(request, capacity=_capacity("capacity"), budget=None, lease=lease)
    duplicate = store.request_spawn(request, capacity=_capacity("capacity"), budget=None, lease=lease)
    conflict = store.request_spawn(
        replace(request, definition_id="different"), capacity=_capacity("capacity"), budget=None, lease=lease
    )
    assert first.disposition is SpawnAdvanceDisposition.APPLIED
    assert duplicate.disposition is SpawnAdvanceDisposition.IDEMPOTENT
    assert conflict.disposition is SpawnAdvanceDisposition.CONFLICT


def test_orphan_path_collision_generation_rollback_and_stale_fence_fail_closed(tmp_path):
    now = [1.0]
    coordinator = InMemoryLeaseCoordinator(clock=lambda: now[0])
    stale = coordinator.acquire("lineage", "old", 1)
    store = AgentLineageStore(tmp_path / "lineage.json", lease_coordinator=coordinator)
    store.register_root("root", "root-definition", lease=stale)
    now[0] = 3.0
    current = coordinator.acquire("lineage", "new", 30)
    orphan = SpawnRequest("orphan", "root", "missing", "/root/o", None, "definition", "cap-o", ())
    assert (
        store.request_spawn(orphan, capacity=_capacity("cap-o"), budget=None, lease=current).disposition
        is SpawnAdvanceDisposition.CONFLICT
    )
    request = SpawnRequest("ok", "root", "root", "/root/a", "a", "definition", "cap", ())
    active = _activate(store, request, current)
    assert (
        store.authorize_incarnation(
            active.logical_agent_id or "",
            incarnation_generation=1,
            fencing_token=current.fencing_token,
        ).disposition
        is LineageAuthorizationDisposition.AUTHORIZED
    )
    assert (
        store.authorize_incarnation(
            active.logical_agent_id or "",
            incarnation_generation=0,
            fencing_token=current.fencing_token,
        ).disposition
        is LineageAuthorizationDisposition.INCARNATION_MISMATCH
    )
    assert (
        store.authorize_incarnation(
            active.logical_agent_id or "",
            incarnation_generation=1,
            fencing_token=stale.fencing_token,
        ).disposition
        is LineageAuthorizationDisposition.STALE_FENCE
    )
    collision = replace(request, request_id="collision", capacity_reservation_id="cap-2")
    assert (
        store.request_spawn(collision, capacity=_capacity("cap-2"), budget=None, lease=current).disposition
        is SpawnAdvanceDisposition.CONFLICT
    )
    pending = _advance(store, active, SpawnLifecycle.PLACEMENT_PENDING, current, placement="worker-2")
    rollback = store.advance(
        "ok",
        SpawnLifecycle.INCARNATION_STARTED,
        expected_revision=pending.revision,
        lease=current,
        placement="worker-2",
        incarnation_generation=1,
    )
    assert rollback.disposition is SpawnAdvanceDisposition.CONFLICT
    with pytest.raises(Exception):
        store.advance(
            "ok",
            SpawnLifecycle.INCARNATION_STARTED,
            expected_revision=pending.revision,
            lease=stale,
            placement="worker-2",
            incarnation_generation=2,
        )


def test_lineage_codec_rejects_unknown_schema_extra_fields_and_wrong_primitives(tmp_path):
    coordinator = InMemoryLeaseCoordinator()
    lease = coordinator.acquire("lineage", "owner", 30)
    store = AgentLineageStore(tmp_path / "lineage.json", lease_coordinator=coordinator)
    record = store.register_root("root", "definition", lease=lease)
    encoded = encode_lineage((record,), (), record.revision)
    revision, decoded, admissions = decode_lineage(encoded)
    assert revision == record.revision and decoded == (record,) and admissions == ()
    for corrupted in (
        encoded.replace(b'"mote.agent-lineage/v3"', b'"unknown"'),
        encoded.replace(b'"tombstoned":false', b'"tombstoned":0'),
        encoded[:-1] + b',"extra":1}',
    ):
        with pytest.raises(ValueError):
            decode_lineage(corrupted)
