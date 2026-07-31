import asyncio
from datetime import datetime, timedelta, timezone

from mote.contracts.inference.shared import CallerIncarnation, SharedSessionCredential
from mote.product.inference.daemon.execution_backend import SharedEmbeddedExecutionBackend
from mote.product.inference.daemon.rpc import gateway_v1_pb2 as pb
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

    async def append_event(self, event):
        self.events[(event.execution_id, event.sequence)] = event
        return event

    async def read_events(self, execution_id, *, after_sequence, limit=256):
        return tuple(
            event
            for (observed_id, sequence), event in sorted(self.events.items())
            if observed_id == execution_id and sequence > after_sequence
        )[:limit]


def _credential(principal):
    now = datetime.now(timezone.utc)
    return SharedSessionCredential(
        session_id="shared-session",
        protocol_version=3,
        socket_generation="socket-generation",
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
        backend = SharedEmbeddedExecutionBackend(
            unary=runtime,
            commands=unused,
            sessions=unused,
            transfers=unused,
            receipts=receipts,
            session_receipts=_NoSessions(),
            events=_Events(),
        )
        canonical = _request(stream=True)
        credential = _credential(canonical.principal)
        envelope = pb.Envelope(
            schema_version=1,
            protocol_version=3,
            generation_id=canonical.generation_id,
            generation_artifact_digest=canonical.generation_artifact_digest,
        )
        revision = await backend.start_unary(
            pb.StartRequest(
                envelope=envelope,
                execution_id=canonical.attempt_id,
                operation="chat.complete",
                canonical_request=canonical.model_dump_json().encode(),
            ),
            credential,
        )
        assert revision == 1
        await backend.authorize(
            pb.AuthorizeRequest(
                envelope=envelope,
                execution_id=canonical.attempt_id,
                wire_permit=_permit(canonical).model_dump_json().encode(),
            ),
            credential,
        )
        events = [
            event.event_type
            async for event in backend.stream_events(
                pb.CursorRequest(
                    envelope=envelope,
                    execution_id=canonical.attempt_id,
                    after_sequence=0,
                ),
                credential,
            )
        ]
        receipt = await backend.query_receipt(
            pb.ReceiptRequest(envelope=envelope, execution_id=canonical.attempt_id),
            credential,
        )
        assert events[-1] == "succeeded"
        assert receipt.state == "terminal_succeeded"
        assert transport.calls == 1
        await backend.aclose()

    asyncio.run(scenario())
