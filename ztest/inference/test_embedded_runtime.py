import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from mote.contracts.inference.attempt import InferenceAttemptRequest
from mote.contracts.inference.deadline import CrossProcessDeadline
from mote.contracts.inference.events import AttemptEventType
from mote.contracts.inference.governance import (
    BudgetReservation,
    ProviderQuotaObservation,
    ReservationState,
    UsageSettlement,
)
from mote.contracts.inference.identity import InferencePrincipal, TrustedSchedulingClass
from mote.contracts.inference.receipt import ReceiptState, validate_receipt_transition
from mote.contracts.inference.transport import ProviderWireResult
from mote.contracts.inference.wire_permit import ExecutionTaxonomy, WirePermit
from mote.contracts.model.failover import EndpointDescriptor
from mote.runtime.inference.governance import CredentialHealthAuthority, ProviderQuotaAuthority
from mote.runtime.inference.runtime import EmbeddedInferenceRuntime
from mote.runtime.models.inference_attempt_executor import InferenceAttemptExecutor

DIGEST = "sha256:" + "e" * 64


class MemoryReceipts:
    def __init__(self):
        self.receipts = {}

    async def accept(self, receipt):
        key = (receipt.attempt_id, receipt.generation_id)
        self.receipts.setdefault(key, receipt)
        return self.receipts[key]

    async def get(self, attempt_id, generation_id):
        return self.receipts.get((attempt_id, generation_id))

    async def compare_and_swap(self, receipt, *, expected_revision, fencing_token):
        key = (receipt.attempt_id, receipt.generation_id)
        current = self.receipts[key]
        assert current.revision == expected_revision
        assert receipt.fencing_token == fencing_token
        validate_receipt_transition(current, receipt)
        self.receipts[key] = receipt
        return receipt


class FakeGenerateTransport:
    def __init__(self, *, fail_after_wire=False, usage_units=None, payload=None):
        self.calls = 0
        self.fail_after_wire = fail_after_wire
        self.usage_units = usage_units
        self.payload = payload or {"text": "ok"}

    async def generate_once(self, request, *, local_deadline, lifecycle, stream):
        self.calls += 1
        assert self.calls == 1
        await lifecycle.wire_started()
        if self.fail_after_wire:
            raise ConnectionError("lost after wire")
        await lifecycle.response_started()
        if stream is not None:
            await stream.emit({"delta": "ok"})
        return ProviderWireResult(payload=self.payload, usage_units=self.usage_units)

    async def aclose(self):
        return None


class Resolver:
    def __init__(self, transport):
        self.transport = transport

    def resolve_generate(self, request):
        return self.transport


class PermitVerifier:
    async def verify(self, permit):
        return permit.signature == "embedded"


class MemoryUsageLedger:
    def __init__(self):
        self.reservations = {}
        self.settlements = {}

    async def reserve(self, **values):
        reservation = BudgetReservation(
            reservation_id=values["reservation_id"],
            attempt_id=values["attempt_id"],
            tenant_id=values["tenant_id"],
            project_id=values["project_id"],
            units=values["units"],
            fencing_token=1,
            expires_at=datetime.now(timezone.utc) + timedelta(seconds=values["ttl_seconds"]),
        )
        self.reservations[reservation.attempt_id] = reservation
        return reservation

    async def settle(self, reservation, *, settlement_id, actual_units):
        return self._finish(reservation, settlement_id, actual_units, ReservationState.SETTLED)

    async def release(self, reservation, *, settlement_id):
        return self._finish(reservation, settlement_id, 0, ReservationState.RELEASED)

    async def pending_reconciliation(self, reservation, *, settlement_id):
        return self._finish(reservation, settlement_id, 0, ReservationState.PENDING_RECONCILIATION)

    async def reconcile(self, reservation, *, settlement_id, actual_units, fencing_token):
        return self._finish(reservation, settlement_id, actual_units, ReservationState.SETTLED)

    async def reclaim_expired(self, *, now, fencing_token):
        return ()

    def _finish(self, reservation, settlement_id, actual_units, state):
        settlement = UsageSettlement(
            settlement_id=settlement_id,
            reservation_id=reservation.reservation_id,
            attempt_id=reservation.attempt_id,
            actual_units=actual_units,
            state=state,
        )
        self.settlements[reservation.attempt_id] = settlement
        return settlement


def _runtime_kwargs(ledger):
    return {
        "usage_ledger": ledger,
        "reserve_units": lambda request: 100,
        "provider_quota": ProviderQuotaAuthority(),
        "credential_health": CredentialHealthAuthority(),
        "permit_verifier": PermitVerifier(),
    }


class GenerationLease:
    generation_id = "generation-1"
    artifact_digest = DIGEST

    def __init__(self):
        self.released = False

    def release(self):
        assert not self.released
        self.released = True


class Generations:
    def __init__(self):
        self.leases = []

    def acquire(self, domain):
        lease = GenerationLease()
        self.leases.append(lease)
        return lease


def _request(attempt_id="attempt-1", *, stream=False):
    now = datetime.now(timezone.utc)
    return InferenceAttemptRequest(
        model_call_id="call-1",
        owner_journal_id="journal",
        attempt_id=attempt_id,
        generation_id="generation-1",
        generation_artifact_digest=DIGEST,
        endpoint=EndpointDescriptor(
            endpoint_id="endpoint-1",
            transport="openai_chat",
            provider="openai",
            model="model",
            base_url_identity="https://provider.invalid",
            credential_pool_id="pool",
            lifecycle_revision="1",
        ),
        credential_slot_id="slot",
        credential_version="1",
        invocation={"operation": "chat.complete", "messages": []},
        deadline=CrossProcessDeadline(
            deadline_utc=now + timedelta(seconds=5),
            remaining_seconds_at_send=5,
            sent_at_utc=now,
        ),
        stream=stream,
        principal=InferencePrincipal(
            tenant_id="tenant",
            project_id="project",
            subject_id="subject",
            policy_revision="1",
            delegation_digest=DIGEST,
        ),
        scheduling=TrustedSchedulingClass(),
    )


def _permit(request):
    now = datetime.now(timezone.utc)
    return WirePermit(
        attempt_id=request.attempt_id,
        execution_taxonomy=ExecutionTaxonomy.UNARY_FINITE_ATTEMPT,
        owner_journal_id="journal",
        wire_unit="chat.complete",
        generation_id=request.generation_id,
        generation_artifact_digest=request.generation_artifact_digest,
        ordinal=1,
        nonce="0123456789abcdef",
        issued_journal_revision=1,
        not_before=now,
        expires_at=now + timedelta(minutes=1),
        issuer_key_id="key",
        audience="embedded/application/tenant",
        trust_revision=1,
        backup_epoch=0,
        admission_epoch=0,
        signature="embedded",
    )


class PermitIssuer:
    def issue(self, **values):
        return WirePermit(
            **values,
            nonce="0123456789abcdef",
            issuer_key_id="key",
            trust_revision=1,
            signature="embedded",
        )


def test_model_attempt_executor_journals_authorization_before_single_wire():
    async def scenario():
        response = {
            "output": {"kind": "generate", "content": "ok"},
            "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
        }
        transport = FakeGenerateTransport(payload=response, usage_units=2)
        runtime = EmbeddedInferenceRuntime(
            receipts=MemoryReceipts(),
            transports=Resolver(transport),
            generations=Generations(),
            **_runtime_kwargs(MemoryUsageLedger()),
            permit_audience="embedded/application/tenant",
            epoch_provider=lambda: (0, 0),
            worker_count=1,
            global_in_flight=1,
            provider_in_flight=1,
            endpoint_in_flight=1,
        )
        executor = InferenceAttemptExecutor(
            runtime,
            PermitIssuer(),
            permit_audience="embedded/application/tenant",
            epoch_provider=lambda: (0, 0),
        )
        records = []

        async def append(record):
            assert transport.calls == 0
            records.append(record)

        result = await executor.execute(
            _request(),
            ordinal=1,
            resume_generation=0,
            issued_journal_revision=2,
            append_authorization=append,
        )
        assert result.response.output.content == "ok"
        assert records[0].issued_journal_revision == 2
        assert transport.calls == 1
        await runtime.aclose()

    asyncio.run(scenario())


def test_embedded_runtime_waits_for_authorization_and_sends_once():
    async def scenario():
        receipts = MemoryReceipts()
        transport = FakeGenerateTransport()
        ledger = MemoryUsageLedger()
        runtime = EmbeddedInferenceRuntime(
            receipts=receipts,
            transports=Resolver(transport),
            generations=Generations(),
            **_runtime_kwargs(ledger),
            permit_audience="embedded/application/tenant",
            epoch_provider=lambda: (0, 0),
            worker_count=1,
            global_in_flight=1,
            provider_in_flight=1,
            endpoint_in_flight=1,
        )
        request = _request(stream=True)
        execution = await runtime.start_attempt(request)
        events = []
        async for event in execution:
            events.append(event.event_type)
            if event.event_type is AttemptEventType.WIRE_AUTHORIZATION_REQUIRED:
                assert transport.calls == 0
                await execution.authorize_wire(_permit(request))
        await runtime.drain(timeout_seconds=2)
        await runtime.aclose()
        assert transport.calls == 1
        assert events.count(AttemptEventType.WIRE_STARTED) == 1
        assert events[-1] is AttemptEventType.SUCCEEDED
        assert receipts.receipts[(request.attempt_id, request.generation_id)].state is ReceiptState.TERMINAL_SUCCEEDED
        assert ledger.settlements[request.attempt_id].state is ReservationState.PENDING_RECONCILIATION

    asyncio.run(scenario())


def test_embedded_runtime_converges_post_wire_loss_to_in_doubt():
    async def scenario():
        receipts = MemoryReceipts()
        transport = FakeGenerateTransport(fail_after_wire=True)
        ledger = MemoryUsageLedger()
        runtime = EmbeddedInferenceRuntime(
            receipts=receipts,
            transports=Resolver(transport),
            generations=Generations(),
            **_runtime_kwargs(ledger),
            permit_audience="embedded/application/tenant",
            epoch_provider=lambda: (0, 0),
            worker_count=1,
            global_in_flight=1,
            provider_in_flight=1,
            endpoint_in_flight=1,
        )
        request = _request()
        execution = await runtime.start_attempt(request)
        events = []
        async for event in execution:
            events.append(event.event_type)
            if event.event_type is AttemptEventType.WIRE_AUTHORIZATION_REQUIRED:
                await execution.authorize_wire(_permit(request))
        await runtime.aclose()
        assert transport.calls == 1
        assert events[-1] is AttemptEventType.IN_DOUBT
        assert receipts.receipts[(request.attempt_id, request.generation_id)].state is ReceiptState.IN_DOUBT
        assert ledger.settlements[request.attempt_id].state is ReservationState.PENDING_RECONCILIATION

    asyncio.run(scenario())


def test_embedded_runtime_rejects_stale_epoch_before_wire():
    async def scenario():
        receipts = MemoryReceipts()
        transport = FakeGenerateTransport()
        ledger = MemoryUsageLedger()
        runtime = EmbeddedInferenceRuntime(
            receipts=receipts,
            transports=Resolver(transport),
            generations=Generations(),
            **_runtime_kwargs(ledger),
            permit_audience="embedded/application/tenant",
            epoch_provider=lambda: (2, 3),
            worker_count=1,
            global_in_flight=1,
            provider_in_flight=1,
            endpoint_in_flight=1,
        )
        request = _request()
        execution = await runtime.start_attempt(request)
        async for event in execution:
            if event.event_type is AttemptEventType.WIRE_AUTHORIZATION_REQUIRED:
                with pytest.raises(Exception, match="epoch is stale"):
                    await execution.authorize_wire(_permit(request))
                await execution.cancel("stale permit rejected")
        await runtime.aclose()
        assert transport.calls == 0
        assert receipts.receipts[(request.attempt_id, request.generation_id)].state is ReceiptState.TERMINAL_CANCELLED
        assert ledger.settlements[request.attempt_id].state is ReservationState.RELEASED

    asyncio.run(scenario())


def test_embedded_runtime_settles_known_usage_and_fails_closed_on_quota():
    async def scenario():
        receipts = MemoryReceipts()
        ledger = MemoryUsageLedger()
        quota = ProviderQuotaAuthority()
        await quota.observe(
            ProviderQuotaObservation(
                provider="openai",
                endpoint_id="endpoint-1",
                credential_slot_id="slot",
                kind="limits",
                remaining_tokens=50,
            )
        )
        transport = FakeGenerateTransport(usage_units=25)
        kwargs = _runtime_kwargs(ledger)
        kwargs["provider_quota"] = quota
        runtime = EmbeddedInferenceRuntime(
            receipts=receipts,
            transports=Resolver(transport),
            generations=Generations(),
            **kwargs,
            permit_audience="embedded/application/tenant",
            epoch_provider=lambda: (0, 0),
            worker_count=1,
            global_in_flight=1,
            provider_in_flight=1,
            endpoint_in_flight=1,
        )
        execution = await runtime.start_attempt(_request())
        events = [event.event_type async for event in execution]
        await runtime.aclose()
        assert events[-1] is AttemptEventType.FAILED
        assert transport.calls == 0
        assert ledger.reservations == {}

        allowed_quota = ProviderQuotaAuthority()
        kwargs["provider_quota"] = allowed_quota
        runtime = EmbeddedInferenceRuntime(
            receipts=MemoryReceipts(),
            transports=Resolver(transport),
            generations=Generations(),
            **kwargs,
            permit_audience="embedded/application/tenant",
            epoch_provider=lambda: (0, 0),
            worker_count=1,
            global_in_flight=1,
            provider_in_flight=1,
            endpoint_in_flight=1,
        )
        request = _request(attempt_id="attempt-2")
        execution = await runtime.start_attempt(request)
        async for event in execution:
            if event.event_type is AttemptEventType.WIRE_AUTHORIZATION_REQUIRED:
                await execution.authorize_wire(_permit(request))
        await runtime.aclose()
        assert ledger.settlements[request.attempt_id].actual_units == 25

    asyncio.run(scenario())
