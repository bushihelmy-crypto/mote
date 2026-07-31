import asyncio
from datetime import datetime, timedelta, timezone

from mote.contracts.inference.deadline import CrossProcessDeadline
from mote.contracts.inference.events import SessionEventType, SessionLifecycleEvent
from mote.contracts.inference.executions import BoundExecutionRequest, SessionApplicationMessage
from mote.contracts.inference.identity import InferencePrincipal, TrustedSchedulingClass
from mote.contracts.inference.wire_permit import WirePermit
from mote.product.inference.daemon.shared_runtime import SharedSessionRuntime

DIGEST = "sha256:" + "1" * 64


class _RawEvent:
    def __init__(self, event):
        self.execution_id = event.session_id
        self.sequence = event.sequence
        self.receipt_revision = event.receipt_revision
        self.event_type = event.event_type.value
        self.payload = event.model_dump_json().encode()


class _Client:
    def __init__(self):
        self.authorized = []
        self.messages = []
        self.cancelled = []

    def envelope(self, **fields):
        return fields

    async def open_session(self, request, *, timeout=None):
        return type("Response", (), {"execution_id": request.execution_id, "receipt_revision": 1})()

    async def authorize_wire(self, execution_id, permit, *, generation_id, timeout=None):
        self.authorized.append((execution_id, generation_id, permit))

    async def resume_events(self, execution_id, **kwargs):
        for sequence, event_type in (
            (1, SessionEventType.QUEUED),
            (2, SessionEventType.OPEN_AUTHORIZATION_REQUIRED),
            (3, SessionEventType.OPENED),
        ):
            yield _RawEvent(
                SessionLifecycleEvent(
                    session_id=execution_id,
                    sequence=sequence,
                    receipt_revision=sequence,
                    generation_id="generation",
                    event_type=event_type,
                )
            )

    def session(self, requests):
        async def events():
            async for request in requests:
                self.messages.append(request)
                yield _RawEvent(
                    SessionLifecycleEvent(
                        session_id=request.execution_id,
                        sequence=4,
                        receipt_revision=4,
                        generation_id="generation",
                        event_type=SessionEventType.MESSAGE_SENT,
                    )
                )
                yield _RawEvent(
                    SessionLifecycleEvent(
                        session_id=request.execution_id,
                        sequence=5,
                        receipt_revision=5,
                        generation_id="generation",
                        event_type=SessionEventType.CLOSED,
                    )
                )
                return

        return events()

    async def cancel(self, execution_id, reason, *, generation_id, timeout=None):
        self.cancelled.append((execution_id, reason, generation_id))

    async def close(self):
        return None


def _request():
    now = datetime.now(timezone.utc)
    return BoundExecutionRequest(
        execution_id="session",
        owner_journal_id="journal",
        generation_id="generation",
        generation_artifact_digest=DIGEST,
        endpoint_binding_id="binding",
        credential_slot_id="slot",
        credential_version="1",
        operation="realtime.open",
        payload={},
        deadline=CrossProcessDeadline(
            deadline_utc=now + timedelta(seconds=5),
            remaining_seconds_at_send=5,
            sent_at_utc=now,
        ),
        principal=InferencePrincipal(
            tenant_id="tenant",
            project_id="project",
            subject_id="subject",
            policy_revision="policy",
            delegation_digest=DIGEST,
        ),
        scheduling=TrustedSchedulingClass(),
    )


def _permit(attempt_id, ordinal):
    now = datetime.now(timezone.utc)
    return WirePermit(
        attempt_id=attempt_id,
        execution_taxonomy="long_lived_session",
        owner_journal_id="journal",
        wire_unit="realtime",
        generation_id="generation",
        generation_artifact_digest=DIGEST,
        ordinal=ordinal,
        nonce=f"0123456789abcde{ordinal}",
        issued_journal_revision=ordinal,
        not_before=now,
        expires_at=now + timedelta(seconds=10),
        issuer_key_id="key",
        audience="shared/session/tenant",
        trust_revision=1,
        backup_epoch=0,
        admission_epoch=0,
        signature="signed",
    )


def test_shared_session_runtime_preserves_authorization_sequence_and_events():
    async def scenario():
        client = _Client()
        runtime = SharedSessionRuntime(client)
        execution = await runtime.open(_request())
        events = []
        async for event in execution:
            events.append(event.event_type)
            if event.event_type is SessionEventType.OPEN_AUTHORIZATION_REQUIRED:
                await execution.authorize_open(_permit("session", 1))
            elif event.event_type is SessionEventType.OPENED:
                await execution.send(
                    SessionApplicationMessage(
                        session_id="session",
                        sequence=1,
                        message_type="response.create",
                        payload={},
                    ),
                    _permit("session:1", 2),
                )
            elif event.event_type is SessionEventType.CLOSED:
                break
        await runtime.drain(timeout_seconds=1)
        return client, events

    client, events = asyncio.run(scenario())
    assert events == [
        SessionEventType.QUEUED,
        SessionEventType.OPEN_AUTHORIZATION_REQUIRED,
        SessionEventType.OPENED,
        SessionEventType.MESSAGE_SENT,
        SessionEventType.CLOSED,
    ]
    assert len(client.authorized) == 1
    assert len(client.messages) == 1
