import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from mote.contracts.inference.deadline import CrossProcessDeadline
from mote.contracts.inference.executions import BoundExecutionRequest
from mote.contracts.inference.identity import InferencePrincipal, TrustedSchedulingClass
from mote.contracts.inference.transport import ProviderWireResult
from mote.contracts.inference.wire_permit import WirePermit
from mote.product.inference.backends.sqlite import SQLiteAttemptReceiptStore, SQLiteUsageLedger
from mote.runtime.inference.command_runtime import EmbeddedServiceCommandRuntime
from mote.runtime.inference.governance import CredentialHealthAuthority, ProviderQuotaAuthority

DIGEST = "sha256:" + "d" * 64


class _Lease:
    generation_id = "generation"
    artifact_digest = DIGEST

    def __init__(self):
        self.released = False

    def release(self):
        assert not self.released
        self.released = True


class _Generations:
    def __init__(self):
        self.leases = []

    def acquire(self, domain):
        lease = _Lease()
        self.leases.append(lease)
        return lease


class _Transport:
    provider = "provider"
    endpoint_id = "endpoint"
    wire_protocol = "https"

    def __init__(self):
        self.calls = 0

    async def execute_once(self, request, *, local_deadline, lifecycle):
        self.calls += 1
        await lifecycle.wire_started()
        await lifecycle.response_started()
        return ProviderWireResult(payload={"provider_resource_id": "resource-1"}, usage_units=7)

    async def aclose(self):
        return None


class _Resolver:
    def __init__(self, transport):
        self.transport = transport

    def resolve_command(self, request):
        return self.transport


class _PermitVerifier:
    async def verify(self, permit):
        return permit.signature == "embedded"


def _request():
    now = datetime.now(timezone.utc)
    return BoundExecutionRequest(
        execution_id="command-1",
        owner_journal_id="service-journal",
        generation_id="generation",
        generation_artifact_digest=DIGEST,
        endpoint_binding_id="binding",
        credential_slot_id="slot",
        credential_version="1",
        operation="batch.create",
        payload={"input": "artifact://input"},
        deadline=CrossProcessDeadline(
            deadline_utc=now + timedelta(seconds=5),
            remaining_seconds_at_send=5,
            sent_at_utc=now,
        ),
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
        attempt_id=request.execution_id,
        execution_taxonomy="durable_operation",
        owner_journal_id="service-journal",
        wire_unit="batch.create",
        generation_id=request.generation_id,
        generation_artifact_digest=request.generation_artifact_digest,
        ordinal=1,
        nonce="0123456789abcdef",
        issued_journal_revision=1,
        not_before=now,
        expires_at=now + timedelta(minutes=1),
        issuer_key_id="key",
        audience="embedded/service/tenant",
        trust_revision=1,
        backup_epoch=0,
        admission_epoch=0,
        signature="embedded",
    )


def test_command_runtime_uses_durable_taxonomy_and_single_wire(tmp_path):
    async def scenario():
        receipts = SQLiteAttemptReceiptStore(tmp_path / "gateway.sqlite3")
        await receipts.initialize()
        ledger = SQLiteUsageLedger(receipts)
        await ledger.configure_budget("tenant", "project", 100)
        transport = _Transport()
        generations = _Generations()
        runtime = EmbeddedServiceCommandRuntime(
            receipts=receipts,
            usage_ledger=ledger,
            reserve_units=lambda request: 10,
            provider_quota=ProviderQuotaAuthority(),
            credential_health=CredentialHealthAuthority(),
            permit_verifier=_PermitVerifier(),
            transports=_Resolver(transport),
            generations=generations,
            permit_audience="embedded/service/tenant",
            epoch_provider=lambda: (0, 0),
            worker_count=1,
            global_in_flight=1,
            provider_in_flight=1,
            endpoint_in_flight=1,
        )
        request = _request()
        execution = await runtime.start_command(request)
        events = []
        async for event in execution:
            events.append(event.event_type.value)
            if event.event_type.value == "wire_authorization_required":
                await execution.authorize_wire(_permit(request))
        await runtime.drain(timeout_seconds=2)
        await runtime.aclose()
        assert transport.calls == 1
        assert events[-1] == "succeeded"
        assert generations.leases[0].released
        receipt = await receipts.get(request.execution_id, request.generation_id)
        assert receipt is not None and receipt.state.value == "terminal_succeeded"

    asyncio.run(scenario())


def test_command_runtime_drain_rejects_new_work_and_waits_for_terminal(tmp_path):
    async def scenario():
        receipts = SQLiteAttemptReceiptStore(tmp_path / "gateway.sqlite3")
        await receipts.initialize()
        ledger = SQLiteUsageLedger(receipts)
        await ledger.configure_budget("tenant", "project", 100)
        transport = _Transport()
        runtime = EmbeddedServiceCommandRuntime(
            receipts=receipts,
            usage_ledger=ledger,
            reserve_units=lambda request: 10,
            provider_quota=ProviderQuotaAuthority(),
            credential_health=CredentialHealthAuthority(),
            permit_verifier=_PermitVerifier(),
            transports=_Resolver(transport),
            generations=_Generations(),
            permit_audience="embedded/service/tenant",
            epoch_provider=lambda: (0, 0),
            worker_count=1,
            global_in_flight=1,
            provider_in_flight=1,
            endpoint_in_flight=1,
        )
        request = _request()
        execution = await runtime.start_command(request)
        while True:
            event = await anext(execution)
            if event.event_type.value == "wire_authorization_required":
                break
        draining = asyncio.create_task(runtime.drain(timeout_seconds=2))
        await asyncio.sleep(0)
        with pytest.raises(RuntimeError, match="draining"):
            await runtime.start_command(request.model_copy(update={"execution_id": "command-2"}))
        assert not draining.done()
        await execution.cancel("drain")
        await draining
        await runtime.aclose()

    asyncio.run(scenario())
