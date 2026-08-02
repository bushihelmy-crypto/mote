from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest

from mote.contracts.agent.runtime_identity import AgentId, CancellationEpoch, IncarnationGeneration, LineageRevision
from mote.contracts.clock import UNIX_UTC_CLOCK, AbsoluteInstant
from mote.contracts.runtime.errors import LeaseFencedError
from mote.contracts.runtime.operation_ownership import (
    EffectCapability,
    EffectSettlement,
    OperationBackend,
    OperationOwnership,
    OperationOwnershipRequest,
)
from mote.contracts.session.identity import SessionId
from mote.contracts.workflow import TrustedWorkflowBlueprintSource
from mote.contracts.workflow.admission import WorkflowCreateAdmission, WorkflowCreateAdmissionLifecycle
from mote.contracts.workflow.authority import (
    WorkflowCreateAdmissionId,
    WorkflowRunAccessGrant,
    WorkflowRunCreationProvenance,
)
from mote.contracts.workflow.governance import (
    WorkflowGovernanceAcceptanceDisposition,
    WorkflowGovernanceCancelRequest,
    WorkflowGovernanceRunDisposition,
    WorkflowGovernanceSettlementLifecycle,
    WorkflowGovernanceSnapshotVerification,
)
from mote.contracts.workflow.identity import WorkflowDefinitionId, WorkflowRunId, WorkflowRunReference
from mote.orchestration.workflows.durable import (
    CreateWorkflowRun,
    ReconcileState,
    WorkflowGovernanceCancellationInbox,
    WorkflowGovernanceCancellationReconciler,
    WorkflowReconciler,
    WorkflowReconciliationStore,
    WorkflowRunControl,
    WorkflowRunPhase,
    WorkflowRunStore,
)
from mote.product.workflows.durability import ProductWorkflowDurability
from mote.runtime.control.leases import InMemoryLeaseCoordinator
from mote.runtime.control.operation_ownership import LeaseOperationOwnership


def _runtime(tmp_path, *, max_attempts=3):
    now = [AbsoluteInstant(1, UNIX_UTC_CLOCK, 10_000_000_000)]
    ownership = LeaseOperationOwnership(InMemoryLeaseCoordinator(), backend=OperationBackend.LOCAL_FILE)
    store = WorkflowReconciliationStore(tmp_path / "reconcile.json", ownership)
    reconciler = WorkflowReconciler(
        store,
        ownership,
        deployment_id="test",
        holder_id="worker",
        backend=OperationBackend.LOCAL_FILE,
        now=lambda: now[0],
        max_attempts=max_attempts,
    )
    return reconciler, store, now


def test_effect_intent_is_durable_before_execute_and_receipt_settles(tmp_path) -> None:
    reconciler, store, _ = _runtime(tmp_path)
    intent = reconciler.submit_effect(
        WorkflowRunId("run"), "charge", EffectCapability.IDEMPOTENT_BY_KEY, '{"amount":1}'
    )
    observed = []

    def execute(item):
        observed.append(store.records()["effects"][item.effect_id].state)
        return EffectSettlement.SUCCEEDED, "provider-receipt"

    assert reconciler.reconcile_effects(execute) == 1
    settled = store.records()["effects"][intent.effect_id]
    assert observed == [ReconcileState.CLAIMED]
    assert settled.state is ReconcileState.SETTLED
    assert settled.provider_receipt == "provider-receipt"


def test_non_replayable_unknown_result_is_in_doubt_not_retried(tmp_path) -> None:
    reconciler, store, _ = _runtime(tmp_path)
    intent = reconciler.submit_effect(WorkflowRunId("run"), "email", EffectCapability.NON_REPLAYABLE, "{}")

    def fail(_item):
        raise ConnectionError("result unknown")

    reconciler.reconcile_effects(fail)
    assert store.records()["effects"][intent.effect_id].state is ReconcileState.IN_DOUBT
    assert reconciler.reconcile_effects(fail) == 0


def test_retry_has_bounded_schedule_and_poison_dead_letters(tmp_path) -> None:
    reconciler, store, now = _runtime(tmp_path, max_attempts=2)
    intent = reconciler.submit_effect(WorkflowRunId("run"), "safe", EffectCapability.IDEMPOTENT_BY_KEY, "{}")

    def fail(_item):
        raise RuntimeError("poison")

    reconciler.reconcile_effects(fail)
    retry = store.records()["effects"][intent.effect_id]
    assert retry.state is ReconcileState.AVAILABLE
    assert retry.next_eligible_at.epoch_nanoseconds > now[0].epoch_nanoseconds
    assert reconciler.reconcile_effects(fail) == 0
    now[0] = retry.next_eligible_at
    reconciler.reconcile_effects(fail)
    assert store.records()["effects"][intent.effect_id].state is ReconcileState.DEAD_LETTER


def test_terminal_delivery_is_rediscovered_after_restart_and_acked(tmp_path) -> None:
    reconciler, store, now = _runtime(tmp_path)
    delivery = reconciler.submit_terminal(WorkflowRunId("run"), "session", '{"status":"ok"}')
    reopened = WorkflowReconciliationStore(store._path, store._ownership)
    restarted = WorkflowReconciler(
        reopened,
        store._ownership,
        deployment_id="test",
        holder_id="worker",
        backend=OperationBackend.LOCAL_FILE,
        now=lambda: now[0],
    )
    assert restarted.reconcile_deliveries(lambda item: item.delivery_id == delivery.delivery_id) == 1
    assert reopened.records()["deliveries"][delivery.delivery_id].state is ReconcileState.SETTLED


def test_terminal_delivery_is_independent_per_destination(tmp_path) -> None:
    reconciler, store, _ = _runtime(tmp_path)
    left = reconciler.submit_terminal(WorkflowRunId("run"), "left", "done")
    right = reconciler.submit_terminal(WorkflowRunId("run"), "right", "done")
    assert left.delivery_id != right.delivery_id
    reconciler.reconcile_deliveries(lambda item: item.destination_id == "left")
    records = store.records()["deliveries"]
    assert records[left.delivery_id].state is ReconcileState.SETTLED
    assert records[right.delivery_id].state is ReconcileState.AVAILABLE


def _governance_request(epoch: int = 1) -> WorkflowGovernanceCancelRequest:
    root = AgentId("root")
    subtree = AgentId("child")
    cancellation_epoch = CancellationEpoch(epoch)
    return WorkflowGovernanceCancelRequest(
        WorkflowGovernanceCancelRequest.derive_id(root, subtree, cancellation_epoch),
        root,
        subtree,
        LineageRevision(7),
        cancellation_epoch,
        (subtree,),
        (WorkflowCreateAdmissionId("admission-1"),),
    )


class _VerifiedSnapshot:
    def verify(self, _request):
        return WorkflowGovernanceSnapshotVerification.VERIFIED


def test_governance_acceptance_is_durable_idempotent_and_strict(tmp_path) -> None:
    _, store, _ = _runtime(tmp_path)
    inbox = WorkflowGovernanceCancellationInbox(store, _VerifiedSnapshot())
    request = _governance_request()
    accepted = inbox.submit(request)
    assert accepted.disposition is WorkflowGovernanceAcceptanceDisposition.ACCEPTED
    assert inbox.submit(request).disposition is WorkflowGovernanceAcceptanceDisposition.IDEMPOTENT
    snapshot = inbox.get(request.request_id)
    assert snapshot is not None
    assert snapshot.lifecycle is WorkflowGovernanceSettlementLifecycle.PENDING

    reopened = WorkflowReconciliationStore(store._path, store._ownership)
    assert reopened.get(request.request_id) == snapshot


def test_governance_backpressure_does_not_persist_acceptance(tmp_path) -> None:
    now = AbsoluteInstant(1, UNIX_UTC_CLOCK, 10_000_000_000)
    ownership = LeaseOperationOwnership(InMemoryLeaseCoordinator(), backend=OperationBackend.LOCAL_FILE)
    store = WorkflowReconciliationStore(tmp_path / "bounded-reconcile.json", ownership, governance_capacity=1)
    inbox = WorkflowGovernanceCancellationInbox(store, _VerifiedSnapshot())
    assert inbox.submit(_governance_request(1)).disposition is WorkflowGovernanceAcceptanceDisposition.ACCEPTED
    rejected = _governance_request(2)
    receipt = inbox.submit(rejected)
    assert receipt.disposition is WorkflowGovernanceAcceptanceDisposition.BACKPRESSURED
    assert receipt.accepted_revision is None
    assert store.get(rejected.request_id) is None


def test_governance_missing_frozen_admission_retries_then_dead_letters(tmp_path) -> None:
    now = [AbsoluteInstant(1, UNIX_UTC_CLOCK, 10_000_000_000)]
    ownership = LeaseOperationOwnership(InMemoryLeaseCoordinator(), backend=OperationBackend.LOCAL_FILE)
    store = WorkflowReconciliationStore(tmp_path / "governance-retry.json", ownership)
    inbox = WorkflowGovernanceCancellationInbox(store, _VerifiedSnapshot())
    request = _governance_request()
    assert inbox.submit(request).disposition is WorkflowGovernanceAcceptanceDisposition.ACCEPTED
    run_store = WorkflowRunStore(tmp_path / "empty-runs.json", ownership)
    control = WorkflowRunControl(
        run_store,
        ownership,
        deployment_id="test",
        holder_id="run-owner",
        backend=OperationBackend.LOCAL_FILE,
    )
    reconciler = WorkflowGovernanceCancellationReconciler(
        store,
        ownership,
        run_store,
        control,
        _AdmissionFacts(None),
        deployment_id="test",
        holder_id="governance-owner",
        backend=OperationBackend.LOCAL_FILE,
        now=lambda: now[0],
        max_attempts=2,
    )

    assert reconciler.reconcile() == 1
    partial = inbox.get(request.request_id)
    assert partial is not None
    assert partial.lifecycle is WorkflowGovernanceSettlementLifecycle.PARTIAL
    persisted = store.records()["governance_cancellations"][request.request_id]
    now[0] = persisted.next_eligible_at

    assert reconciler.reconcile() == 1
    exhausted = inbox.get(request.request_id)
    assert exhausted is not None
    assert exhausted.lifecycle is WorkflowGovernanceSettlementLifecycle.DEAD_LETTER
    assert reconciler.reconcile() == 0


def test_governance_stale_claim_cannot_commit_settlement(tmp_path) -> None:
    monotonic = [0.0]
    coordinator = InMemoryLeaseCoordinator(clock=lambda: monotonic[0])
    ownership = LeaseOperationOwnership(coordinator, backend=OperationBackend.LOCAL_FILE)
    store = WorkflowReconciliationStore(tmp_path / "governance-fence.json", ownership)
    inbox = WorkflowGovernanceCancellationInbox(store, _VerifiedSnapshot())
    request = _governance_request()
    assert inbox.submit(request).disposition is WorkflowGovernanceAcceptanceDisposition.ACCEPTED
    item = store.records()["governance_cancellations"][request.request_id]
    operation_request = OperationOwnershipRequest(
        "test",
        str(request.request_id),
        "old-owner",
        OperationBackend.LOCAL_FILE,
        item.revision,
        str(request.request_id),
        EffectCapability.IDEMPOTENT_BY_KEY,
    )
    stale = ownership.claim(operation_request, 10)
    monotonic[0] = 11
    current = ownership.claim(replace(operation_request, holder_id="new-owner"), 10)
    try:
        with pytest.raises(LeaseFencedError):
            store.commit_governance(
                replace(item, revision=item.revision + 1),
                item.revision,
                stale,
            )
        assert inbox.get(request.request_id).revision == item.revision
    finally:
        ownership.release(current)


class _AdmissionFacts:
    def __init__(self, admission):
        self._admission = admission

    def get_workflow_create_admission(self, admission_id):
        return self._admission if self._admission is not None and self._admission.admission_id == admission_id else None


class _GovernanceSource(_AdmissionFacts, _VerifiedSnapshot):
    def reserved_workflow_create_admissions(self):
        return ()

    def claim_workflow_create_admission(self, command):
        raise AssertionError("committed admission cannot be claimed")

    def reserve_workflow_create_admission(self, command):
        raise AssertionError("scan cannot reserve admissions")

    def settle_workflow_create_admission(self, command):
        raise AssertionError("committed admission cannot be settled")


def test_governance_reconciler_applies_cancel_intent_without_claiming_terminal(tmp_path) -> None:
    now = AbsoluteInstant(1, UNIX_UTC_CLOCK, 10_000_000_000)
    ownership = LeaseOperationOwnership(InMemoryLeaseCoordinator(), backend=OperationBackend.LOCAL_FILE)
    run_store = WorkflowRunStore(tmp_path / "runs.json", ownership)
    control = WorkflowRunControl(
        run_store,
        ownership,
        deployment_id="test",
        holder_id="run-owner",
        backend=OperationBackend.LOCAL_FILE,
    )
    admission_id = WorkflowCreateAdmissionId("admission-1")
    provenance = WorkflowRunCreationProvenance(
        admission_id,
        AgentId("child"),
        IncarnationGeneration(1),
        LineageRevision(7),
        CancellationEpoch(0),
        SessionId("session"),
        AgentId("root"),
        now,
    )
    command = CreateWorkflowRun(
        "create-1",
        WorkflowDefinitionId("definition"),
        provenance,
        WorkflowRunAccessGrant(AgentId("child"), AgentId("root")),
        TrustedWorkflowBlueprintSource("test.workflow", 1),
        "0" * 64,
        "{}",
    )
    run = control.create(command)
    admission = WorkflowCreateAdmission(
        admission_id,
        "create-1",
        run.reference,
        AgentId("child"),
        AgentId("root"),
        LineageRevision(7),
        CancellationEpoch(0),
        1,
        OperationOwnership(
            OperationOwnershipRequest(
                "test",
                str(admission_id),
                "owner",
                OperationBackend.LOCAL_FILE,
                0,
                f"workflow-create:{admission_id}",
                EffectCapability.NO_EXTERNAL_EFFECT,
            ),
            "owner",
            1,
            30.0,
        ),
        WorkflowCreateAdmissionLifecycle.COMMITTED,
    )
    reconciliation_store = WorkflowReconciliationStore(tmp_path / "reconcile-governance.json", ownership)
    inbox = WorkflowGovernanceCancellationInbox(reconciliation_store, _VerifiedSnapshot())
    request = _governance_request()
    assert inbox.submit(request).disposition is WorkflowGovernanceAcceptanceDisposition.ACCEPTED
    reconciler = WorkflowGovernanceCancellationReconciler(
        reconciliation_store,
        ownership,
        run_store,
        control,
        _AdmissionFacts(admission),
        deployment_id="test",
        holder_id="governance-owner",
        backend=OperationBackend.LOCAL_FILE,
        now=lambda: now,
    )
    assert reconciler.reconcile() == 1
    cancelled = run_store.get(run.reference)
    assert cancelled is not None and cancelled.phase is WorkflowRunPhase.CANCELLING
    snapshot = inbox.get(request.request_id)
    assert snapshot is not None
    assert snapshot.lifecycle is WorkflowGovernanceSettlementLifecycle.SETTLED
    assert snapshot.per_run_settlements[0].disposition.value == "cancel_intent_applied"


def test_governance_reconciler_recovers_after_cancel_before_settlement_commit(tmp_path, monkeypatch) -> None:
    now = AbsoluteInstant(1, UNIX_UTC_CLOCK, 10_000_000_000)
    ownership = LeaseOperationOwnership(InMemoryLeaseCoordinator(), backend=OperationBackend.LOCAL_FILE)
    run_store = WorkflowRunStore(tmp_path / "runs-crash.json", ownership)
    control = WorkflowRunControl(
        run_store,
        ownership,
        deployment_id="test",
        holder_id="run-owner",
        backend=OperationBackend.LOCAL_FILE,
    )
    admission_id = WorkflowCreateAdmissionId("admission-1")
    provenance = WorkflowRunCreationProvenance(
        admission_id,
        AgentId("child"),
        IncarnationGeneration(1),
        LineageRevision(7),
        CancellationEpoch(0),
        SessionId("session"),
        AgentId("root"),
        now,
    )
    run = control.create(
        CreateWorkflowRun(
            "create-1",
            WorkflowDefinitionId("definition"),
            provenance,
            WorkflowRunAccessGrant(AgentId("child"), AgentId("root")),
            TrustedWorkflowBlueprintSource("test.workflow", 1),
            "0" * 64,
            "{}",
        )
    )
    admission = WorkflowCreateAdmission(
        admission_id,
        "create-1",
        run.reference,
        AgentId("child"),
        AgentId("root"),
        LineageRevision(7),
        CancellationEpoch(0),
        1,
        OperationOwnership(
            OperationOwnershipRequest(
                "test",
                str(admission_id),
                "owner",
                OperationBackend.LOCAL_FILE,
                0,
                f"workflow-create:{admission_id}",
                EffectCapability.NO_EXTERNAL_EFFECT,
            ),
            "owner",
            1,
            30.0,
        ),
        WorkflowCreateAdmissionLifecycle.COMMITTED,
    )
    reconciliation_store = WorkflowReconciliationStore(tmp_path / "reconcile-governance-crash.json", ownership)
    inbox = WorkflowGovernanceCancellationInbox(reconciliation_store, _VerifiedSnapshot())
    request = _governance_request()
    assert inbox.submit(request).disposition is WorkflowGovernanceAcceptanceDisposition.ACCEPTED
    reconciler = WorkflowGovernanceCancellationReconciler(
        reconciliation_store,
        ownership,
        run_store,
        control,
        _AdmissionFacts(admission),
        deployment_id="test",
        holder_id="governance-owner",
        backend=OperationBackend.LOCAL_FILE,
        now=lambda: now,
    )
    commit = reconciliation_store.commit_governance
    failures = 0

    def crash_before_settlement(item, expected_revision, operation_ownership):
        nonlocal failures
        failures += 1
        if failures == 1:
            raise RuntimeError("simulated crash before governance settlement commit")
        return commit(item, expected_revision, operation_ownership)

    monkeypatch.setattr(reconciliation_store, "commit_governance", crash_before_settlement)
    with pytest.raises(RuntimeError, match="simulated crash"):
        reconciler.reconcile()
    cancelling = run_store.get(run.reference)
    assert cancelling is not None
    assert cancelling.phase is WorkflowRunPhase.CANCELLING
    assert cancelling.revision == run.revision + 1
    pending = inbox.get(request.request_id)
    assert pending is not None
    assert pending.lifecycle is WorkflowGovernanceSettlementLifecycle.PENDING

    assert reconciler.reconcile() == 1
    recovered = run_store.get(run.reference)
    assert recovered is not None
    assert recovered.phase is WorkflowRunPhase.CANCELLING
    assert recovered.revision == cancelling.revision
    snapshot = inbox.get(request.request_id)
    assert snapshot is not None
    assert snapshot.lifecycle is WorkflowGovernanceSettlementLifecycle.SETTLED
    assert snapshot.per_run_settlements[0].disposition is WorkflowGovernanceRunDisposition.ALREADY_CANCELLING


def test_product_activation_periodically_rediscovers_terminal_delivery(tmp_path) -> None:
    async def scenario() -> None:
        root = tmp_path / "product-workflows"
        first = ProductWorkflowDurability(root, scan_interval_seconds=0.01)
        first.reconciler.submit_terminal(WorkflowRunId("run"), "agent:a:task:1", "done")
        await first.aclose()

        delivered: list[str] = []
        restarted = ProductWorkflowDurability(root, scan_interval_seconds=0.01)
        assert restarted.pending_terminal_destinations("agent:a:") == ("agent:a:task:1",)
        restarted.register_terminal_deliverer(
            "agent:a:task:1",
            lambda delivery: not delivered.append(delivery.outcome_payload),
        )
        await restarted.start()
        for _ in range(20):
            if delivered:
                break
            await asyncio.sleep(0.01)
        await restarted.aclose()
        assert delivered == ["done"]
        assert restarted.pending_terminal_destinations("agent:a:") == ()

    asyncio.run(scenario())


def test_product_scan_activates_durable_governance_cancellation(tmp_path) -> None:
    async def scenario() -> None:
        durability = ProductWorkflowDurability(tmp_path / "product-governance", scan_interval_seconds=0.01)
        now = AbsoluteInstant(1, UNIX_UTC_CLOCK, 10_000_000_000)
        admission_id = WorkflowCreateAdmissionId("admission-1")
        provenance = WorkflowRunCreationProvenance(
            admission_id,
            AgentId("child"),
            IncarnationGeneration(1),
            LineageRevision(7),
            CancellationEpoch(0),
            SessionId("session"),
            AgentId("root"),
            now,
        )
        run = durability.control.create(
            CreateWorkflowRun(
                "create-1",
                WorkflowDefinitionId("definition"),
                provenance,
                WorkflowRunAccessGrant(AgentId("child"), AgentId("root")),
                TrustedWorkflowBlueprintSource("test.workflow", 1),
                "0" * 64,
                "{}",
            )
        )
        admission = WorkflowCreateAdmission(
            admission_id,
            "create-1",
            run.reference,
            AgentId("child"),
            AgentId("root"),
            LineageRevision(7),
            CancellationEpoch(0),
            1,
            OperationOwnership(
                OperationOwnershipRequest(
                    "test",
                    str(admission_id),
                    "owner",
                    OperationBackend.LOCAL_FILE,
                    0,
                    f"workflow-create:{admission_id}",
                    EffectCapability.NO_EXTERNAL_EFFECT,
                ),
                "owner",
                1,
                30.0,
            ),
            WorkflowCreateAdmissionLifecycle.COMMITTED,
        )
        source = _GovernanceSource(admission)
        durability.register_agent_governance(AgentId("root"), source, source)
        request = _governance_request()
        assert durability.submit(request).disposition is WorkflowGovernanceAcceptanceDisposition.ACCEPTED
        await durability.start()
        try:
            for _ in range(50):
                snapshot = durability.get(request.request_id)
                if snapshot is not None and snapshot.lifecycle is WorkflowGovernanceSettlementLifecycle.SETTLED:
                    break
                await asyncio.sleep(0.01)
            else:
                raise AssertionError("governance cancellation was not reconciled")
            cancelled = durability.query(run.reference)
            assert cancelled is not None
            assert cancelled.phase is WorkflowRunPhase.CANCELLING
        finally:
            durability.unregister_agent_governance(AgentId("root"))
            await durability.aclose()

    asyncio.run(scenario())
