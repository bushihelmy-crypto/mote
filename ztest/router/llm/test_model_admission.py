from __future__ import annotations

import pytest

from mote.contracts.config.model.breaker import BreakerConfig
from mote.contracts.model.failover import (
    AdmissionGate,
    CredentialVerdict,
    FailureDisposition,
    FailureDomain,
    FailureReason,
    HealthVerdict,
    OperatorState,
    QuotaObservation,
    ResourceIdentity,
    Retryability,
)
from mote.contracts.model.invocation import ModelQuotaObservation
from mote.runtime.resilience.admission import ResourceAdmissionController


class _MemoryOperatorAudit:
    def __init__(self) -> None:
        self.items = []

    def append(self, transition) -> None:
        self.items.append(transition)

    def records(self):
        return tuple(self.items)


def _resource(
    *,
    slot: str = "slot-a",
    tenant: str = "tenant-a",
    endpoint: str = "endpoint-a",
) -> ResourceIdentity:
    return ResourceIdentity(
        endpoint_id=endpoint,
        transport="openai",
        endpoint_fingerprint=f"fingerprint:{endpoint}",
        model_or_deployment="model",
        tenant_fingerprint=tenant,
        credential_slot_id=slot,
    )


def _failure(
    reason: FailureReason,
    domain: FailureDomain,
    verdict: HealthVerdict,
) -> FailureDisposition:
    return FailureDisposition(
        reason=reason,
        domain=domain,
        retryability=Retryability.NEW_ATTEMPT,
        health_verdict=verdict,
        credential_verdict=(
            CredentialVerdict.QUARANTINE if reason is FailureReason.AUTH_REJECTED else CredentialVerdict.NEUTRAL
        ),
        quota_observation=(
            QuotaObservation.RETRY_AFTER if reason is FailureReason.RATE_LIMITED else QuotaObservation.NONE
        ),
    )


def _permit(controller: ResourceAdmissionController, resource: ResourceIdentity):
    result = controller.acquire(resource, remaining_seconds=30)
    assert result.rejection is None
    assert result.permit is not None
    return result.permit


def test_credential_failure_does_not_trip_endpoint_availability() -> None:
    now = [0.0]
    controller = ResourceAdmissionController(
        breaker_config=BreakerConfig(min_samples=1, error_rate_threshold=1.0),
        clock=lambda: now[0],
    )
    bad = _resource()
    other = _resource(slot="slot-b", tenant="tenant-b")

    _permit(controller, bad).fail(
        _failure(
            FailureReason.AUTH_REJECTED,
            FailureDomain.CREDENTIAL,
            HealthVerdict.NEUTRAL,
        )
    )

    rejected = controller.acquire(bad, remaining_seconds=30)
    assert rejected.rejection is not None
    assert rejected.rejection.gate is AdmissionGate.CREDENTIAL
    _permit(controller, other).abandon()


def test_quota_and_availability_use_independent_resource_planes() -> None:
    now = [0.0]
    controller = ResourceAdmissionController(
        breaker_config=BreakerConfig(min_samples=1, error_rate_threshold=1.0),
        clock=lambda: now[0],
    )
    first = _resource()
    same_account = _resource(slot="slot-b")
    other_account = _resource(slot="slot-c", tenant="tenant-c")

    _permit(controller, first).fail(
        _failure(
            FailureReason.RATE_LIMITED,
            FailureDomain.QUOTA,
            HealthVerdict.NEUTRAL,
        )
    )

    quota = controller.acquire(same_account, remaining_seconds=30)
    assert quota.rejection is not None
    assert quota.rejection.gate is AdmissionGate.QUOTA
    _permit(controller, other_account).fail(
        _failure(
            FailureReason.CONNECTION,
            FailureDomain.TRANSPORT,
            HealthVerdict.DEGRADE,
        )
    )

    availability = controller.acquire(
        _resource(slot="slot-d", tenant="tenant-d"),
        remaining_seconds=30,
    )
    assert availability.rejection is not None
    assert availability.rejection.gate is AdmissionGate.AVAILABILITY


def test_bulkhead_releases_on_abandon_and_permit_settles_once() -> None:
    controller = ResourceAdmissionController(max_in_flight_per_endpoint=1)
    resource = _resource()
    first = _permit(controller, resource)

    rejected = controller.acquire(resource, remaining_seconds=30)
    assert rejected.rejection is not None
    assert rejected.rejection.gate is AdmissionGate.BULKHEAD

    first.abandon()
    with pytest.raises(RuntimeError, match="already settled"):
        first.succeed()
    _permit(controller, resource).succeed()


def test_operator_and_deadline_gates_precede_resource_admission() -> None:
    audit = _MemoryOperatorAudit()
    controller = ResourceAdmissionController(operator_audit=audit)
    resource = _resource()
    controller.transition_operator_state(
        resource,
        OperatorState.DRAINING,
        expected_revision=0,
        config_revision="config-1",
        actor="test",
        reason="maintenance",
    )

    draining = controller.acquire(resource, remaining_seconds=30)
    assert draining.rejection is not None
    assert draining.rejection.gate is AdmissionGate.OPERATOR

    controller.transition_operator_state(
        resource,
        OperatorState.ENABLED,
        expected_revision=1,
        config_revision="config-1",
        actor="test",
        reason="maintenance complete",
    )
    deadline = controller.acquire(resource, remaining_seconds=0)
    assert deadline.rejection is not None
    assert deadline.rejection.gate is AdmissionGate.DEADLINE


def test_half_open_probe_abandon_releases_lease() -> None:
    now = [0.0]
    controller = ResourceAdmissionController(
        breaker_config=BreakerConfig(
            min_samples=1,
            error_rate_threshold=1.0,
            open_seconds=1.0,
        ),
        clock=lambda: now[0],
    )
    resource = _resource()
    _permit(controller, resource).fail(
        _failure(
            FailureReason.CONNECTION,
            FailureDomain.TRANSPORT,
            HealthVerdict.DEGRADE,
        )
    )
    now[0] = 1.0

    probe = _permit(controller, resource)
    saturated = controller.acquire(resource, remaining_seconds=30)
    assert saturated.rejection is not None
    assert saturated.rejection.gate is AdmissionGate.AVAILABILITY
    assert saturated.rejection.reason == "availability recovery probes saturated"

    probe.abandon()
    _permit(controller, resource).succeed()


def test_request_quota_reservation_prevents_concurrent_over_admission() -> None:
    now = [0.0]
    controller = ResourceAdmissionController(clock=lambda: now[0])
    resource = _resource()
    controller.observe_quota(
        resource,
        ModelQuotaObservation(
            remaining_requests=1,
            reset_requests_after_seconds=10.0,
        ),
    )

    reserved = _permit(controller, resource)
    rejected = controller.acquire(resource, remaining_seconds=30)
    assert rejected.rejection is not None
    assert rejected.rejection.gate is AdmissionGate.QUOTA
    assert rejected.rejection.reason == "request quota exhausted"
    assert rejected.rejection.disposition.quota_observation is QuotaObservation.RETRY_AFTER

    reserved.abandon()
    _permit(controller, resource).abandon()


def test_success_observation_reconciles_reservation_and_reset_reopens_gate() -> None:
    now = [0.0]
    controller = ResourceAdmissionController(clock=lambda: now[0])
    resource = _resource()
    controller.observe_quota(
        resource,
        ModelQuotaObservation(
            remaining_requests=1,
            reset_requests_after_seconds=5.0,
        ),
    )
    permit = _permit(controller, resource)
    controller.observe_quota(
        resource,
        ModelQuotaObservation(
            remaining_requests=0,
            reset_requests_after_seconds=5.0,
        ),
    )
    permit.succeed()

    exhausted = controller.acquire(resource, remaining_seconds=30)
    assert exhausted.rejection is not None
    assert exhausted.rejection.gate is AdmissionGate.QUOTA

    now[0] = 5.0
    _permit(controller, resource).abandon()


def test_token_depletion_and_quota_domains_are_isolated() -> None:
    controller = ResourceAdmissionController()
    depleted = _resource()
    other_tenant = _resource(slot="slot-b", tenant="tenant-b")
    controller.observe_quota(
        depleted,
        ModelQuotaObservation(
            remaining_tokens=0,
            reset_tokens_after_seconds=10.0,
        ),
    )

    rejected = controller.acquire(depleted, remaining_seconds=30)
    assert rejected.rejection is not None
    assert rejected.rejection.gate is AdmissionGate.QUOTA
    assert rejected.rejection.reason == "token quota exhausted"
    _permit(controller, other_tenant).abandon()


def test_zero_quota_without_reset_uses_bounded_probe_cooldown() -> None:
    now = [0.0]
    controller = ResourceAdmissionController(
        default_quota_cooldown_seconds=2.0,
        clock=lambda: now[0],
    )
    resource = _resource()
    controller.observe_quota(
        resource,
        ModelQuotaObservation(remaining_requests=0),
    )

    rejected = controller.acquire(resource, remaining_seconds=30)
    assert rejected.rejection is not None
    assert rejected.rejection.disposition.quota_observation is QuotaObservation.RETRY_AFTER
    now[0] = 2.0
    _permit(controller, resource).abandon()
