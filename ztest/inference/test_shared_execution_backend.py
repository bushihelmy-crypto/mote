import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from mote.contracts.inference.execution_owner import (
    ExecutionEpochBinding,
    ExecutionId,
    ExecutionOwnerRecord,
    SharedExecutionVariant,
)
from mote.contracts.inference.executions import SessionApplicationMessage
from mote.contracts.inference.shared import CallerIncarnation, SharedSessionCredential
from mote.contracts.inference.wire_permit import ExecutionTaxonomy
from mote.product.inference.daemon.execution_backend import SharedEmbeddedExecutionBackend, _EventJournal
from mote.product.inference.daemon.messages import (
    AuthorizeExecutionCommand,
    CancelExecutionCommand,
    EventCursor,
    ExecutionQuery,
    RpcEnvelopeBinding,
    SessionMessageCommand,
    StartExecutionCommand,
)
from mote.runtime.inference.runtime import EmbeddedInferenceRuntime
from mote.ztest.inference.test_embedded_runtime import (
    FakeGenerateTransport,
    Generations,
    MemoryReceipts,
    MemoryUsageLedger,
    Resolver,
    _permit,
    _request,
    _runtime_kwargs,
)


class _UnusedRuntime:
    async def aclose(self):
        return None


class _NoSessions:
    async def get(self, session_id, generation_id):
        return None


class _Events:
    def __init__(self):
        self.events = {}
        self.owners = {}

    async def append_event(self, event):
        self.events[(event.execution_id, event.sequence)] = event
        return event

    async def read_events(self, execution_id, *, after_sequence, limit=256):
        return tuple(
            event
            for (observed_id, sequence), event in sorted(self.events.items())
            if observed_id == execution_id and sequence > after_sequence
        )[:limit]

    async def put_owner_record(self, record):
        existing = self.owners.get(record.execution_id)
        if existing is not None and existing != record:
            raise ValueError("owner conflict")
        self.owners[record.execution_id] = record
        return record

    async def get_owner_record(self, execution_id):
        return self.owners.get(execution_id)


def _credential(principal, *, application_id="application-1", session_id="shared-session"):
    now = datetime.now(timezone.utc)
    return SharedSessionCredential(
        session_id=session_id,
        protocol_version=3,
        socket_generation="socket-generation",
        application_id=application_id,
        caller=CallerIncarnation(pid=1, process_start_ticks=1, boot_id="boot"),
        principal=principal,
        issued_at=now,
        expires_at=now + timedelta(minutes=1),
        key_id="key",
        permit_issuer_key_id="permit-key",
        permit_trust_revision=1,
        permit_private_key="a" * 43,
        signature="signature",
    )


def test_shared_backend_delegates_unary_without_replaying_wire():
    async def scenario():
        receipts = MemoryReceipts()
        transport = FakeGenerateTransport()
        runtime = EmbeddedInferenceRuntime(
            receipts=receipts,
            transports=Resolver(transport),
            generations=Generations(),
            **_runtime_kwargs(MemoryUsageLedger()),
            permit_audience="embedded/application/tenant",
            epoch_provider=lambda: (0, 0),
            worker_count=1,
        )
        unused = _UnusedRuntime()
        owner_events = _Events()
        backend = SharedEmbeddedExecutionBackend(
            unary=runtime,
            commands=unused,
            sessions=unused,
            transfers=unused,
            receipts=receipts,
            session_receipts=_NoSessions(),
            events=owner_events,
            owners=owner_events,
            epoch_provider=lambda: (0, 0),
        )
        canonical = _request(stream=True)
        credential = _credential(canonical.principal)
        envelope = RpcEnvelopeBinding(
            generation_id=canonical.generation_id,
            generation_artifact_digest=canonical.generation_artifact_digest,
        )
        revision = await backend.start_unary(
            StartExecutionCommand(
                envelope=envelope,
                execution_id=canonical.attempt_id,
                operation="chat.complete",
                canonical_request=canonical.model_dump_json().encode(),
            ),
            credential,
        )
        assert revision == 1
        await backend.authorize(
            AuthorizeExecutionCommand(
                envelope=envelope,
                execution_id=canonical.attempt_id,
                permit=_permit(canonical),
            ),
            credential,
        )
        events = [
            event.event_type
            async for event in backend.stream_events(
                EventCursor(
                    envelope=envelope,
                    execution_id=canonical.attempt_id,
                    after_sequence=0,
                    receipt_revision=1,
                ),
                credential,
            )
        ]
        receipt = await backend.query_receipt(
            ExecutionQuery(envelope=envelope, execution_id=canonical.attempt_id),
            credential,
        )
        assert events[-1] == "succeeded"
        assert receipt.state == "terminal_succeeded"
        assert transport.calls == 1

        intruder_principal = canonical.principal.model_copy(update={"subject_id": "intruder"})
        intruder = _credential(intruder_principal, session_id="intruder-session")
        with pytest.raises(PermissionError):
            await backend.authorize(
                AuthorizeExecutionCommand(
                    envelope=envelope,
                    execution_id=canonical.attempt_id,
                    permit=_permit(canonical),
                ),
                intruder,
            )
        with pytest.raises(PermissionError):
            await backend.cancel(
                CancelExecutionCommand(envelope=envelope, execution_id=canonical.attempt_id, reason="intrude"),
                intruder,
            )
        with pytest.raises(PermissionError):
            await backend.query_receipt(
                ExecutionQuery(envelope=envelope, execution_id=canonical.attempt_id),
                intruder,
            )
        with pytest.raises(PermissionError):
            await anext(
                backend.stream_events(
                    EventCursor(
                        envelope=envelope, execution_id=canonical.attempt_id, after_sequence=0, receipt_revision=1
                    ),
                    intruder,
                )
            )
        with pytest.raises(PermissionError):
            await anext(
                backend.reconcile(
                    ExecutionQuery(envelope=envelope, execution_id=canonical.attempt_id),
                    intruder,
                )
            )

        backend._executions.clear()
        backend._journals.clear()
        cold_events = [
            event.event_type
            async for event in backend.stream_events(
                EventCursor(envelope=envelope, execution_id=canonical.attempt_id, after_sequence=0, receipt_revision=1),
                credential,
            )
        ]
        assert cold_events[-1] == "succeeded"
        cold_receipt = await backend.query_receipt(
            ExecutionQuery(envelope=envelope, execution_id=canonical.attempt_id),
            credential,
        )
        assert cold_receipt.state == "terminal_succeeded"

        session_id = "session-object-1"
        await owner_events.put_owner_record(
            ExecutionOwnerRecord(
                record_revision=1,
                execution_id=ExecutionId(session_id),
                variant=SharedExecutionVariant.SESSION,
                principal=canonical.principal,
                application_scope=credential.application_id,
                credential_scope=credential.session_id,
                generation_id=canonical.generation_id,
                generation_artifact_digest=canonical.generation_artifact_digest,
                epoch=ExecutionEpochBinding(
                    backup_epoch=0,
                    admission_epoch=0,
                    permit_trust_revision=1,
                ),
            )
        )
        backend._journals[session_id] = _EventJournal(terminal=True)
        session_permit = _permit(canonical).model_copy(
            update={
                "attempt_id": session_id,
                "execution_taxonomy": ExecutionTaxonomy.LONG_LIVED_SESSION,
            }
        )

        async def intruder_messages():
            yield SessionMessageCommand(
                envelope=envelope,
                execution_id=session_id,
                application_sequence=1,
                message=SessionApplicationMessage(
                    session_id=session_id,
                    sequence=1,
                    message_type="input",
                    payload={},
                ),
                permit=session_permit,
            )

        with pytest.raises(PermissionError):
            async for _event in backend.session(intruder_messages(), intruder):
                pass
        await backend.aclose()

    asyncio.run(scenario())
