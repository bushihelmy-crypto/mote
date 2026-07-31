from __future__ import annotations

import asyncio
from collections import deque
from pathlib import Path
from typing import Any

import pytest

from mote.contracts.model.failover import (
    AdmissionGate,
    AdmissionVerdict,
    AttemptBudget,
    AttemptState,
    FailureDisposition,
    FailureDomain,
    FailureReason,
    HealthVerdict,
    ResourceIdentity,
    Retryability,
)
from mote.contracts.service import (
    ServiceAcceptance,
    ServiceAccepted,
    ServiceAttemptFinishedRecord,
    ServiceCompleted,
    ServiceEndpointDescriptor,
    ServiceEndpointFailure,
    ServiceExecutionSemantics,
    ServiceFailed,
    ServiceInvocation,
    ServiceReceipt,
    ServiceResponse,
)
from mote.contracts.service.errors import ServiceCallExhaustedError, ServiceCallInDoubtError
from mote.runtime.resilience.admission import AdmissionResult
from mote.runtime.service_gateway import (
    LocalServiceCallJournal,
    RuntimeServiceGateway,
    ServiceFailoverGroup,
    ServiceFailoverPlanner,
    ServiceRuntimeSnapshot,
)


def _transient() -> FailureDisposition:
    return FailureDisposition(
        reason=FailureReason.CONNECTION,
        domain=FailureDomain.TRANSPORT,
        retryability=Retryability.NEW_ATTEMPT,
        health_verdict=HealthVerdict.DEGRADE,
    )


class _Adapter:
    endpoint_id = "endpoint-a"
    credential_slot_id = "slot-a"
    tenant_fingerprint = "tenant-a"

    def __init__(
        self,
        starts: list[Any],
        polls: list[Any] | None = None,
        *,
        endpoint_id: str = "endpoint-a",
        credential_slot_id: str = "slot-a",
    ) -> None:
        self.endpoint_id = endpoint_id
        self.credential_slot_id = credential_slot_id
        self.starts = deque(starts)
        self.polls = deque(polls or [])
        self.start_count = 0
        self.poll_count = 0
        self.reconcile_count = 0

    async def start_once(self, invocation, endpoint, *, timeout_seconds):
        self.start_count += 1
        outcome = self.starts.popleft()
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    async def poll_once(self, receipt, endpoint, *, timeout_seconds):
        self.poll_count += 1
        outcome = self.polls.popleft()
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    async def reconcile_once(self, invocation, endpoint, *, timeout_seconds):
        self.reconcile_count += 1
        return None

    async def cancel_once(self, receipt, endpoint, *, timeout_seconds):
        return None

    def classify_start(self, exc: Exception) -> ServiceEndpointFailure:
        return ServiceEndpointFailure(
            disposition=_transient(),
            acceptance=ServiceAcceptance.UNKNOWN,
        )

    def classify_poll(self, exc: Exception) -> ServiceEndpointFailure:
        return ServiceEndpointFailure(
            disposition=_transient(),
            acceptance=ServiceAcceptance.UNKNOWN,
        )

    async def aclose(self) -> None:
        return None


class _Resolver:
    def __init__(self, adapter: _Adapter) -> None:
        self.adapter = adapter

    def resolve(self, endpoint, credential_slot_id):
        if credential_slot_id != self.adapter.credential_slot_id:
            return None
        return self.adapter

    async def aclose(self) -> None:
        return None


class _MapResolver:
    def __init__(self, adapters: tuple[_Adapter, ...]) -> None:
        self.adapters = {(adapter.endpoint_id, adapter.credential_slot_id): adapter for adapter in adapters}

    def resolve(self, endpoint, credential_slot_id):
        return self.adapters.get((endpoint.endpoint_id, credential_slot_id))

    async def aclose(self) -> None:
        return None


class _CrashAfterAttemptFinish:
    def __init__(self, journal: LocalServiceCallJournal, state: AttemptState) -> None:
        self._journal = journal
        self._state = state
        self._crashed = False

    async def append(self, record) -> None:
        await self._journal.append(record)
        if not self._crashed and isinstance(record, ServiceAttemptFinishedRecord) and record.state is self._state:
            self._crashed = True
            raise RuntimeError("injected process window")

    def records(self, service_call_id):
        return self._journal.records(service_call_id)

    def recover(self, service_call_id):
        return self._journal.recover(service_call_id)


def _gateway(
    tmp_path: Path,
    adapter: _Adapter,
    *,
    admission_controller: Any = None,
    journal: Any = None,
) -> RuntimeServiceGateway:
    endpoint = ServiceEndpointDescriptor(
        endpoint_id="endpoint-a",
        capability="media.generate.image",
        transport="https",
        provider="fake",
        base_url_identity="base-a",
        credential_pool_id="pool-a",
        lifecycle_revision="revision-a",
    )
    snapshot = ServiceRuntimeSnapshot(
        revision="snapshot-a",
        endpoints=(endpoint,),
        groups=(
            ServiceFailoverGroup(
                group_id="media.image",
                endpoint_ids=(endpoint.endpoint_id,),
                budget=AttemptBudget(
                    max_wire_attempts=3,
                    max_attempts_per_endpoint=3,
                    max_endpoint_switches=0,
                    max_credential_rotations=0,
                    max_request_transforms=0,
                    total_deadline_seconds=10,
                    single_attempt_timeout_seconds=5,
                    max_backoff_seconds=0,
                ),
            ),
        ),
        route_groups=(("media.image", "media.image"),),
        credential_slots=((endpoint.endpoint_id, ("slot-a",)),),
    )
    return RuntimeServiceGateway(
        ServiceFailoverPlanner(snapshot),
        _Resolver(adapter),
        service_call_journal=journal or LocalServiceCallJournal(tmp_path),
        admission_controller=admission_controller,
    )


def _multi_gateway(
    tmp_path: Path,
    left: _Adapter,
    right: _Adapter,
) -> RuntimeServiceGateway:
    endpoints = tuple(
        ServiceEndpointDescriptor(
            endpoint_id=adapter.endpoint_id,
            capability="media.generate.image",
            transport="https",
            provider="fake",
            base_url_identity=adapter.endpoint_id,
            credential_pool_id=f"pool-{adapter.endpoint_id}",
            lifecycle_revision="revision-a",
        )
        for adapter in (left, right)
    )
    snapshot = ServiceRuntimeSnapshot(
        revision="snapshot-multi",
        endpoints=endpoints,
        groups=(
            ServiceFailoverGroup(
                group_id="media.image",
                endpoint_ids=tuple(item.endpoint_id for item in endpoints),
                budget=AttemptBudget(
                    max_wire_attempts=2,
                    max_attempts_per_endpoint=1,
                    max_endpoint_switches=1,
                    max_credential_rotations=0,
                    max_request_transforms=0,
                    total_deadline_seconds=10,
                    single_attempt_timeout_seconds=5,
                    max_backoff_seconds=0,
                ),
            ),
        ),
        route_groups=(("media.image", "media.image"),),
        credential_slots=tuple((adapter.endpoint_id, (adapter.credential_slot_id,)) for adapter in (left, right)),
    )
    return RuntimeServiceGateway(
        ServiceFailoverPlanner(snapshot),
        _MapResolver((left, right)),
        service_call_journal=LocalServiceCallJournal(tmp_path),
    )


def _credential_gateway(
    tmp_path: Path,
    first: _Adapter,
    second: _Adapter,
) -> RuntimeServiceGateway:
    endpoint = ServiceEndpointDescriptor(
        endpoint_id="endpoint-a",
        capability="media.generate.image",
        transport="https",
        provider="fake",
        base_url_identity="endpoint-a",
        credential_pool_id="pool-a",
        lifecycle_revision="revision-a",
    )
    snapshot = ServiceRuntimeSnapshot(
        revision="snapshot-credentials",
        endpoints=(endpoint,),
        groups=(
            ServiceFailoverGroup(
                group_id="media.image",
                endpoint_ids=(endpoint.endpoint_id,),
                budget=AttemptBudget(
                    max_wire_attempts=2,
                    max_attempts_per_endpoint=2,
                    max_endpoint_switches=0,
                    max_credential_rotations=1,
                    max_request_transforms=0,
                    total_deadline_seconds=10,
                    single_attempt_timeout_seconds=5,
                    max_backoff_seconds=0,
                ),
            ),
        ),
        route_groups=(("media.image", "media.image"),),
        credential_slots=((endpoint.endpoint_id, ("slot-a", "slot-b")),),
    )
    return RuntimeServiceGateway(
        ServiceFailoverPlanner(snapshot),
        _MapResolver((first, second)),
        service_call_journal=LocalServiceCallJournal(tmp_path),
    )


def _invocation(
    call_id: str,
    semantics: ServiceExecutionSemantics = ServiceExecutionSemantics.IDEMPOTENT,
) -> ServiceInvocation:
    return ServiceInvocation(
        service_call_id=call_id,
        route_id="media.image",
        capability="media.generate.image",
        payload={"item": {"filename": "a.png"}},
        semantics=semantics,
        idempotency_key=f"key-{call_id}",
    )


@pytest.mark.asyncio
async def test_one_shot_success_is_checkpointed(tmp_path: Path) -> None:
    adapter = _Adapter([ServiceCompleted(response=ServiceResponse(value={"url": "u"}))])
    gateway = _gateway(tmp_path, adapter)

    first = await gateway.execute(_invocation("one-shot"))
    replay = await gateway.execute(_invocation("one-shot"))

    assert first.response.value == {"url": "u"}
    assert replay == first
    assert adapter.start_count == 1


@pytest.mark.asyncio
async def test_receipt_poll_failure_never_resubmits(tmp_path: Path) -> None:
    receipt = ServiceReceipt(provider_operation_id="remote-1", poll_after_seconds=0)
    adapter = _Adapter(
        [ServiceAccepted(receipt=receipt)],
        [
            ConnectionError("poll transport"),
            ServiceAccepted(receipt=receipt),
            ServiceCompleted(response=ServiceResponse(value={"url": "u"})),
        ],
    )
    gateway = _gateway(tmp_path, adapter)

    result = await gateway.execute(_invocation("poll-retry"))

    assert result.response.value == {"url": "u"}
    assert adapter.start_count == 1
    assert adapter.poll_count == 3


@pytest.mark.asyncio
async def test_receipt_survives_cancelled_process_window(tmp_path: Path) -> None:
    receipt = ServiceReceipt(provider_operation_id="remote-2", poll_after_seconds=0)
    first_adapter = _Adapter(
        [ServiceAccepted(receipt=receipt)],
        [asyncio.CancelledError()],
    )
    with pytest.raises(asyncio.CancelledError):
        await _gateway(tmp_path, first_adapter).execute(_invocation("resume-receipt"))

    resumed_adapter = _Adapter(
        [],
        [ServiceCompleted(response=ServiceResponse(value={"url": "restored"}))],
    )
    result = await _gateway(tmp_path, resumed_adapter).resume(_invocation("resume-receipt"))

    assert result.response.value == {"url": "restored"}
    assert resumed_adapter.start_count == 0
    assert resumed_adapter.poll_count == 1


@pytest.mark.asyncio
async def test_non_repeatable_unknown_submit_becomes_in_doubt(tmp_path: Path) -> None:
    adapter = _Adapter([ConnectionError("lost submit response")])
    gateway = _gateway(tmp_path, adapter)

    with pytest.raises(ServiceCallInDoubtError):
        await gateway.execute(
            _invocation(
                "unsafe",
                semantics=ServiceExecutionSemantics.NON_REPEATABLE,
            )
        )

    recovery = gateway._journal.recover("unsafe")
    assert recovery.terminal is not None
    assert recovery.terminal.state.value == "in_doubt"
    assert adapter.start_count == 1


@pytest.mark.asyncio
async def test_concurrent_calls_keep_attempt_cursors_isolated(tmp_path: Path) -> None:
    adapter = _Adapter(
        [
            ServiceCompleted(response=ServiceResponse(value="a")),
            ServiceCompleted(response=ServiceResponse(value="b")),
        ]
    )
    gateway = _gateway(tmp_path, adapter)

    left, right = await asyncio.gather(
        gateway.execute(_invocation("left")),
        gateway.execute(_invocation("right")),
    )

    assert {left.response.value, right.response.value} == {"a", "b"}
    assert adapter.start_count == 2


@pytest.mark.asyncio
async def test_definitive_rejection_can_fallback_endpoint(tmp_path: Path) -> None:
    unavailable = FailureDisposition(
        reason=FailureReason.MODEL_UNAVAILABLE,
        domain=FailureDomain.PROVIDER,
        retryability=Retryability.NEW_ATTEMPT,
    )
    left = _Adapter(
        [ServiceFailed(failure=unavailable)],
        endpoint_id="endpoint-a",
        credential_slot_id="slot-a",
    )
    right = _Adapter(
        [ServiceCompleted(response=ServiceResponse(value="fallback"))],
        endpoint_id="endpoint-b",
        credential_slot_id="slot-b",
    )

    result = await _multi_gateway(tmp_path, left, right).execute(_invocation("definitive-fallback"))

    assert result.response.value == "fallback"
    assert left.start_count == 1
    assert right.start_count == 1


@pytest.mark.asyncio
async def test_unknown_idempotent_submit_does_not_cross_endpoint(
    tmp_path: Path,
) -> None:
    left = _Adapter(
        [ConnectionError("unknown")],
        endpoint_id="endpoint-a",
        credential_slot_id="slot-a",
    )
    right = _Adapter(
        [ServiceCompleted(response=ServiceResponse(value="unsafe-fallback"))],
        endpoint_id="endpoint-b",
        credential_slot_id="slot-b",
    )

    with pytest.raises(ServiceCallExhaustedError):
        await _multi_gateway(tmp_path, left, right).execute(_invocation("unknown-no-cross"))

    assert left.start_count == 1
    assert right.start_count == 0


@pytest.mark.asyncio
async def test_admission_rejection_does_not_consume_wire_attempt(
    tmp_path: Path,
) -> None:
    class RejectingAdmission:
        def acquire(self, resource: ResourceIdentity, *, remaining_seconds: float):
            return AdmissionResult(
                rejection=AdmissionVerdict(
                    gate=AdmissionGate.BULKHEAD,
                    reason="busy",
                    resource=resource,
                    disposition=FailureDisposition(
                        reason=FailureReason.OVERLOADED,
                        domain=FailureDomain.PROVIDER,
                        retryability=Retryability.NEW_ATTEMPT,
                    ),
                )
            )

    adapter = _Adapter([ServiceCompleted(response=ServiceResponse(value="must-not-run"))])
    gateway = _gateway(
        tmp_path,
        adapter,
        admission_controller=RejectingAdmission(),
    )

    with pytest.raises(ServiceCallExhaustedError):
        await gateway.execute(_invocation("admission-rejected"))

    assert adapter.start_count == 0
    assert gateway._journal.recover("admission-rejected").attempt_starts == ()


@pytest.mark.asyncio
async def test_auth_rejection_rotates_credential_without_switching_endpoint(
    tmp_path: Path,
) -> None:
    class AuthRejectedAdapter(_Adapter):
        def classify_start(self, exc: Exception) -> ServiceEndpointFailure:
            return ServiceEndpointFailure(
                disposition=FailureDisposition(
                    reason=FailureReason.AUTH_REJECTED,
                    domain=FailureDomain.CREDENTIAL,
                    retryability=Retryability.NEW_ATTEMPT,
                ),
                acceptance=ServiceAcceptance.REJECTED,
            )

    first = AuthRejectedAdapter(
        [PermissionError("bad key")],
        credential_slot_id="slot-a",
    )
    second = _Adapter(
        [ServiceCompleted(response=ServiceResponse(value="rotated"))],
        credential_slot_id="slot-b",
    )

    result = await _credential_gateway(tmp_path, first, second).execute(_invocation("credential-rotation"))

    assert result.response.value == "rotated"
    assert first.start_count == 1
    assert second.start_count == 1


@pytest.mark.asyncio
async def test_resume_heals_success_checkpoint_window_without_new_wire(
    tmp_path: Path,
) -> None:
    journal = LocalServiceCallJournal(tmp_path)
    first = _Adapter([ServiceCompleted(response=ServiceResponse(value="checkpointed"))])
    with pytest.raises(RuntimeError, match="injected process window"):
        await _gateway(
            tmp_path,
            first,
            journal=_CrashAfterAttemptFinish(journal, AttemptState.SUCCEEDED),
        ).execute(_invocation("heal-success"))

    resumed = _Adapter([])
    result = await _gateway(tmp_path, resumed, journal=journal).resume(_invocation("heal-success"))

    assert result.response.value == "checkpointed"
    assert resumed.start_count == 0


@pytest.mark.asyncio
async def test_resume_never_resubmits_failed_accepted_operation_window(
    tmp_path: Path,
) -> None:
    journal = LocalServiceCallJournal(tmp_path)
    receipt = ServiceReceipt(provider_operation_id="failed-remote", poll_after_seconds=0)
    remote_failure = FailureDisposition(
        reason=FailureReason.SERVER_ERROR,
        domain=FailureDomain.PROVIDER,
        retryability=Retryability.NEW_ATTEMPT,
    )
    first = _Adapter(
        [ServiceAccepted(receipt=receipt)],
        [ServiceFailed(failure=remote_failure)],
    )
    with pytest.raises(RuntimeError, match="injected process window"):
        await _gateway(
            tmp_path,
            first,
            journal=_CrashAfterAttemptFinish(journal, AttemptState.FAILED),
        ).execute(_invocation("heal-accepted-failure"))

    resumed = _Adapter([])
    with pytest.raises(ServiceCallExhaustedError):
        await _gateway(tmp_path, resumed, journal=journal).resume(_invocation("heal-accepted-failure"))

    assert resumed.start_count == 0
    assert resumed.poll_count == 0


@pytest.mark.asyncio
async def test_resume_heals_non_repeatable_in_doubt_window(tmp_path: Path) -> None:
    journal = LocalServiceCallJournal(tmp_path)
    first = _Adapter([ConnectionError("unknown submit")])
    invocation = _invocation(
        "heal-in-doubt",
        semantics=ServiceExecutionSemantics.NON_REPEATABLE,
    )
    with pytest.raises(RuntimeError, match="injected process window"):
        await _gateway(
            tmp_path,
            first,
            journal=_CrashAfterAttemptFinish(journal, AttemptState.IN_DOUBT),
        ).execute(invocation)

    resumed = _Adapter([])
    with pytest.raises(ServiceCallInDoubtError):
        await _gateway(tmp_path, resumed, journal=journal).resume(invocation)

    assert resumed.start_count == 0
