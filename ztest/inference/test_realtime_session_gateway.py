import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from mote.contracts.inference.deadline import CrossProcessDeadline
from mote.contracts.inference.events import SessionEventType, SessionLifecycleEvent
from mote.contracts.inference.executions import BoundExecutionRequest
from mote.contracts.inference.identity import InferencePrincipal, TrustedSchedulingClass
from mote.contracts.inference.wire_permit import WirePermit
from mote.product.inference.session_gateway import RuntimeSessionGateway

DIGEST = "sha256:" + "a" * 64


def _request(payload):
    now = datetime.now(timezone.utc)
    return BoundExecutionRequest(
        execution_id="realtime-1",
        owner_journal_id="journal-1",
        generation_id="generation-1",
        generation_artifact_digest=DIGEST,
        endpoint_binding_id="endpoint-1",
        credential_slot_id="slot-1",
        credential_version="1",
        operation="realtime.open",
        payload=payload,
        deadline=CrossProcessDeadline(
            deadline_utc=now + timedelta(minutes=1),
            remaining_seconds_at_send=60,
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


class _Issuer:
    def __init__(self):
        self.calls = []

    def issue(self, **values):
        self.calls.append(values)
        return WirePermit(
            nonce=f"0123456789abcdef{len(self.calls)}",
            issuer_key_id="key",
            trust_revision=1,
            signature="signature",
            **values,
        )


class _Execution:
    def __init__(self):
        self.events = asyncio.Queue()
        self.permits = []
        self.messages = []
        self.closed = []
        self.events.put_nowait(
            SessionLifecycleEvent(
                session_id="realtime-1",
                sequence=1,
                receipt_revision=1,
                generation_id="generation-1",
                event_type=SessionEventType.QUEUED,
            )
        )
        self.events.put_nowait(
            SessionLifecycleEvent(
                session_id="realtime-1",
                sequence=2,
                receipt_revision=1,
                generation_id="generation-1",
                event_type=SessionEventType.OPEN_AUTHORIZATION_REQUIRED,
            )
        )

    def __aiter__(self):
        return self

    async def __anext__(self):
        return await self.events.get()

    async def authorize_open(self, permit):
        self.permits.append(permit)
        await self.events.put(
            SessionLifecycleEvent(
                session_id="realtime-1",
                sequence=3,
                receipt_revision=6,
                generation_id="generation-1",
                event_type=SessionEventType.OPENED,
            )
        )

    async def send(self, message, permit):
        self.messages.append(message)
        self.permits.append(permit)
        await self.events.put(
            SessionLifecycleEvent(
                session_id="realtime-1",
                sequence=4,
                receipt_revision=9,
                generation_id="generation-1",
                event_type=SessionEventType.MESSAGE_SENT,
                payload={"application_sequence": message.sequence},
            )
        )

    async def close(self, reason):
        self.closed.append(reason)


class _Runtime:
    def __init__(self, execution):
        self.execution = execution
        self.requests = []

    async def open(self, request):
        self.requests.append(request)
        return self.execution


def test_gateway_authorizes_open_and_every_application_message():
    async def scenario():
        execution = _Execution()
        runtime = _Runtime(execution)
        issuer = _Issuer()
        gateway = RuntimeSessionGateway(
            runtime,
            issuer,
            _request,
            permit_audience="session/tenant",
            epoch_provider=lambda: (2, 3),
        )
        session = await gateway.open({"model": "realtime"})
        initial = [await anext(session), await anext(session), await anext(session)]
        await session.send(sequence=1, message_type="response.create", payload={"response": {}})
        sent = await anext(session)
        await session.close("complete")
        return runtime, issuer, execution, initial, sent

    runtime, issuer, execution, initial, sent = asyncio.run(scenario())
    assert runtime.requests[0].payload == {"model": "realtime"}
    assert [event.event_type for event in initial] == [
        SessionEventType.QUEUED,
        SessionEventType.OPEN_AUTHORIZATION_REQUIRED,
        SessionEventType.OPENED,
    ]
    assert sent.event_type is SessionEventType.MESSAGE_SENT
    assert [call["attempt_id"] for call in issuer.calls] == ["realtime-1", "realtime-1:1"]
    assert all(call["execution_taxonomy"] == "long_lived_session" for call in issuer.calls)
    assert issuer.calls[1]["issued_journal_revision"] == 6
    assert issuer.calls[1]["backup_epoch"] == 2
    assert issuer.calls[1]["admission_epoch"] == 3
    assert execution.messages[0].payload == {"response": {}}
    assert execution.closed == ["complete"]


def test_gateway_rejects_out_of_order_sequence_before_runtime_send():
    async def scenario():
        execution = _Execution()
        session = await RuntimeSessionGateway(
            _Runtime(execution),
            _Issuer(),
            _request,
            permit_audience="session/tenant",
            epoch_provider=lambda: (0, 0),
        ).open({"model": "realtime"})
        with pytest.raises(ValueError, match="sequence is not next"):
            await session.send(sequence=2, message_type="response.create", payload={})
        return execution

    assert asyncio.run(scenario()).messages == []
