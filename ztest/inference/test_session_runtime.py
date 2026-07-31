import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import pytest

from mote.contracts.inference.deadline import CrossProcessDeadline
from mote.contracts.inference.events import SessionEventType
from mote.contracts.inference.executions import BoundExecutionRequest, SessionApplicationMessage
from mote.contracts.inference.identity import InferencePrincipal, TrustedSchedulingClass
from mote.contracts.inference.transport import ProviderWireResult
from mote.contracts.inference.wire_permit import WirePermit
from mote.product.inference.backends.sqlite import (
    SQLiteAttemptReceiptStore,
    SQLiteSessionReceiptStore,
    SQLiteUsageLedger,
)
from mote.runtime.inference.governance import CredentialHealthAuthority, ProviderQuotaAuthority
from mote.runtime.inference.session_runtime import EmbeddedSessionRuntime

DIGEST = "sha256:" + "1" * 64


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


class _Connection:
    def __init__(self):
        self.messages = []
        self.closed = False
        self._inbound = asyncio.Queue()

    async def send_once(self, message, *, local_deadline, lifecycle):
        self.messages.append(message)
        await lifecycle.wire_started()
        await lifecycle.response_started()
        return ProviderWireResult(payload={"accepted": message.sequence}, usage_units=3)

    async def _iterate(self):
        while True:
            yield await self._inbound.get()

    def inbound(self):
        return self._iterate()

    async def close(self, reason):
        self.closed = True


@dataclass
class _OpenResult:
    connection: _Connection
    wire_result: ProviderWireResult


class _Transport:
    provider = "provider"
    endpoint_id = "endpoint"
    wire_protocol = "websocket"

    def __init__(self):
        self.connection = _Connection()
        self.opens = 0

    async def open_once(self, request, *, local_deadline, lifecycle):
        self.opens += 1
        await lifecycle.wire_started()
        await lifecycle.response_started()
        return _OpenResult(
            self.connection,
            ProviderWireResult(payload={"session": request.execution_id}, usage_units=2),
        )

    async def aclose(self):
        return None


class _Resolver:
    def __init__(self, transport):
        self.transport = transport

    def resolve_session(self, request):
        return self.transport


class _Verifier:
    async def verify(self, permit):
        return permit.signature == "embedded"


def _request():
    now = datetime.now(timezone.utc)
    return BoundExecutionRequest(
        execution_id="session-1",
        owner_journal_id="session-journal",
        generation_id="generation",
        generation_artifact_digest=DIGEST,
        endpoint_binding_id="binding",
        credential_slot_id="slot",
        credential_version="1",
        operation="realtime.open",
        payload={"model": "realtime"},
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


def _permit(wire_id, wire_unit, ordinal):
    now = datetime.now(timezone.utc)
    return WirePermit(
        attempt_id=wire_id,
        execution_taxonomy="long_lived_session",
        owner_journal_id="session-journal",
        wire_unit=wire_unit,
        generation_id="generation",
        generation_artifact_digest=DIGEST,
        ordinal=ordinal,
        nonce=f"0123456789abcde{ordinal}",
        issued_journal_revision=ordinal,
        not_before=now,
        expires_at=now + timedelta(minutes=1),
        issuer_key_id="key",
        audience="embedded/session/tenant",
        trust_revision=1,
        backup_epoch=0,
        admission_epoch=0,
        signature="embedded",
    )


def test_session_runtime_authorizes_open_and_each_application_message(tmp_path):
    async def scenario():
        authority = SQLiteAttemptReceiptStore(tmp_path / "gateway.sqlite3")
        await authority.initialize()
        ledger = SQLiteUsageLedger(authority)
        await ledger.configure_budget("tenant", "project", 100)
        transport = _Transport()
        generations = _Generations()
        runtime = EmbeddedSessionRuntime(
            session_receipts=SQLiteSessionReceiptStore(authority),
            wire_receipts=authority,
            usage_ledger=ledger,
            reserve_open_units=lambda request: 10,
            reserve_message_units=lambda message: 10,
            provider_quota=ProviderQuotaAuthority(),
            credential_health=CredentialHealthAuthority(),
            permit_verifier=_Verifier(),
            transports=_Resolver(transport),
            generations=generations,
            permit_audience="embedded/session/tenant",
            epoch_provider=lambda: (0, 0),
            worker_count=1,
            global_in_flight=1,
            provider_in_flight=1,
            endpoint_in_flight=1,
        )
        request = _request()
        execution = await runtime.open(request)
        events = []
        async for event in execution:
            events.append(event.event_type)
            if event.event_type is SessionEventType.OPEN_AUTHORIZATION_REQUIRED:
                await execution.authorize_open(_permit(request.execution_id, request.operation, 1))
            elif event.event_type is SessionEventType.OPENED:
                message = SessionApplicationMessage(
                    session_id=request.execution_id,
                    sequence=1,
                    message_type="response.create",
                    payload={"response": {}},
                )
                await execution.send(
                    message,
                    _permit(f"{request.execution_id}:1", message.message_type, 2),
                )
            elif event.event_type is SessionEventType.MESSAGE_SENT:
                await execution.close("complete")
        await runtime.drain(timeout_seconds=2)
        await runtime.aclose()
        assert transport.opens == 1
        assert len(transport.connection.messages) == 1
        assert transport.connection.closed
        assert events[-1] is SessionEventType.CLOSED
        assert generations.leases[0].released

    asyncio.run(scenario())


def test_session_runtime_drain_rejects_new_sessions_and_waits_for_terminal(tmp_path):
    async def scenario():
        authority = SQLiteAttemptReceiptStore(tmp_path / "gateway.sqlite3")
        await authority.initialize()
        ledger = SQLiteUsageLedger(authority)
        await ledger.configure_budget("tenant", "project", 100)
        transport = _Transport()
        runtime = EmbeddedSessionRuntime(
            session_receipts=SQLiteSessionReceiptStore(authority),
            wire_receipts=authority,
            usage_ledger=ledger,
            reserve_open_units=lambda request: 10,
            reserve_message_units=lambda message: 10,
            provider_quota=ProviderQuotaAuthority(),
            credential_health=CredentialHealthAuthority(),
            permit_verifier=_Verifier(),
            transports=_Resolver(transport),
            generations=_Generations(),
            permit_audience="embedded/session/tenant",
            epoch_provider=lambda: (0, 0),
            worker_count=1,
            global_in_flight=1,
            provider_in_flight=1,
            endpoint_in_flight=1,
        )
        request = _request()
        execution = await runtime.open(request)
        while True:
            event = await anext(execution)
            if event.event_type is SessionEventType.OPEN_AUTHORIZATION_REQUIRED:
                await execution.authorize_open(_permit(request.execution_id, request.operation, 1))
            elif event.event_type is SessionEventType.OPENED:
                break
        draining = asyncio.create_task(runtime.drain(timeout_seconds=2))
        await asyncio.sleep(0)
        with pytest.raises(RuntimeError, match="draining"):
            await runtime.open(request.model_copy(update={"execution_id": "session-2"}))
        assert not draining.done()
        await execution.close("complete")
        await draining
        await runtime.aclose()

    asyncio.run(scenario())
