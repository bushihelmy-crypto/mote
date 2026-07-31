from __future__ import annotations

import asyncio

import pytest

from mote.contracts.model.failover import OperatorState, ResourceIdentity
from mote.runtime.resilience.admission import ResourceAdmissionController
from mote.runtime.resilience.failover.operator import (
    LocalModelOperatorAuditStore,
    OperatorAuditIntegrityError,
    OperatorAuditRequiredError,
    OperatorDrainIncompleteError,
    OperatorRevisionConflict,
)


def _resource() -> ResourceIdentity:
    return ResourceIdentity(
        endpoint_id="primary",
        transport="openai",
        endpoint_fingerprint="endpoint-fingerprint",
        model_or_deployment="model",
        tenant_fingerprint="tenant",
        credential_slot_id="slot",
    )


def _permit(controller: ResourceAdmissionController):
    result = controller.acquire(_resource(), remaining_seconds=30)
    assert result.rejection is None
    assert result.permit is not None
    return result.permit


def _transition(
    controller: ResourceAdmissionController,
    state: OperatorState,
    revision: int,
    *,
    force: bool = False,
):
    return controller.transition_operator_state(
        _resource(),
        state,
        expected_revision=revision,
        config_revision="config-1",
        actor="operator:test",
        reason=f"move to {state.value}",
        force=force,
    )


@pytest.mark.asyncio
async def test_draining_rejects_new_calls_and_waits_for_existing_permit(
    tmp_path,
) -> None:
    audit = LocalModelOperatorAuditStore(tmp_path / "operator.jsonl")
    controller = ResourceAdmissionController(operator_audit=audit)
    permit = _permit(controller)

    transition = _transition(controller, OperatorState.DRAINING, 0)
    assert transition.in_flight == 1
    rejected = controller.acquire(_resource(), remaining_seconds=30)
    assert rejected.rejection is not None
    assert rejected.rejection.reason == OperatorState.DRAINING.value

    waiter = asyncio.create_task(controller.wait_drained(_resource()))
    await asyncio.sleep(0)
    assert not waiter.done()
    permit.succeed()
    status = await waiter

    assert status.state is OperatorState.DRAINING
    assert status.drained is True
    assert status.in_flight == 0


def test_control_revision_and_audit_restore_state(tmp_path) -> None:
    audit = LocalModelOperatorAuditStore(tmp_path / "operator.jsonl")
    controller = ResourceAdmissionController(operator_audit=audit)
    _transition(controller, OperatorState.DRAINING, 0)
    _transition(controller, OperatorState.DISABLED, 1)

    restored = ResourceAdmissionController(operator_audit=LocalModelOperatorAuditStore(audit.path))
    status = restored.operator_status(_resource())
    assert status.state is OperatorState.DISABLED
    assert status.control_revision == 2
    assert status.drained is True

    enabled = _transition(restored, OperatorState.ENABLED, 2)
    assert enabled.control_revision == 3
    assert len(audit.records()) == 3


def test_stale_revision_and_unaudited_control_are_rejected(tmp_path) -> None:
    audit = LocalModelOperatorAuditStore(tmp_path / "operator.jsonl")
    controller = ResourceAdmissionController(operator_audit=audit)
    _transition(controller, OperatorState.DRAINING, 0)

    with pytest.raises(OperatorRevisionConflict):
        _transition(controller, OperatorState.ENABLED, 0)
    with pytest.raises(OperatorAuditRequiredError):
        _transition(
            ResourceAdmissionController(),
            OperatorState.DRAINING,
            0,
        )
    assert len(audit.records()) == 1


@pytest.mark.asyncio
async def test_disable_requires_completed_drain_unless_forced(tmp_path) -> None:
    audit = LocalModelOperatorAuditStore(tmp_path / "operator.jsonl")
    controller = ResourceAdmissionController(operator_audit=audit)
    permit = _permit(controller)
    _transition(controller, OperatorState.DRAINING, 0)

    with pytest.raises(OperatorDrainIncompleteError):
        _transition(controller, OperatorState.DISABLED, 1)
    forced = _transition(controller, OperatorState.DISABLED, 1, force=True)
    assert forced.force is True
    assert controller.operator_status(_resource()).drained is False

    permit.abandon()
    status = await controller.wait_drained(_resource(), timeout_seconds=1)
    assert status.state is OperatorState.DISABLED
    assert status.drained is True


def test_audit_failure_does_not_change_control_state() -> None:
    class BrokenAudit:
        def append(self, transition) -> None:
            raise OSError("disk full")

        def records(self):
            return ()

    controller = ResourceAdmissionController(operator_audit=BrokenAudit())
    with pytest.raises(OSError, match="disk full"):
        _transition(controller, OperatorState.DRAINING, 0)

    status = controller.operator_status(_resource())
    assert status.state is OperatorState.ENABLED
    assert status.control_revision == 0


def test_incomplete_audit_record_fails_closed_on_restore(tmp_path) -> None:
    path = tmp_path / "operator.jsonl"
    path.write_bytes(b'{"schema_version":1')

    with pytest.raises(OperatorAuditIntegrityError, match="incomplete"):
        ResourceAdmissionController(operator_audit=LocalModelOperatorAuditStore(path))
