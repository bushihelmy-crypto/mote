import asyncio
from datetime import datetime, timedelta, timezone

from aiohttp import WSMessage, WSMsgType

from mote.contracts.inference.deadline import CrossProcessDeadline
from mote.contracts.inference.executions import BoundExecutionRequest, SessionApplicationMessage
from mote.contracts.inference.identity import InferencePrincipal, TrustedSchedulingClass
from mote.product.models.transports.openai_realtime import OpenAIRealtimeTransport

DIGEST = "sha256:" + "a" * 64


class _Socket:
    def __init__(self):
        self.sent = []
        self.messages = [
            WSMessage(WSMsgType.TEXT, '{"type":"response.done"}', None),
            WSMessage(WSMsgType.CLOSE, None, None),
        ]
        self.closed = False

    async def send_bytes(self, value):
        self.sent.append(value)

    def __aiter__(self):
        async def messages():
            for message in self.messages:
                yield message

        return messages()

    async def close(self, *, code, message):
        self.closed = True


class _Session:
    def __init__(self, socket):
        self.socket = socket
        self.calls = []

    async def ws_connect(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.socket


class _Lease:
    def __init__(self, session):
        self.session = session
        self.released = False

    async def release(self):
        self.released = True


class _Lifecycle:
    def __init__(self):
        self.started = 0
        self.responded = 0

    async def wire_started(self):
        self.started += 1

    async def response_started(self):
        self.responded += 1


def _request():
    now = datetime.now(timezone.utc)
    return BoundExecutionRequest(
        execution_id="session",
        owner_journal_id="journal",
        generation_id="generation",
        generation_artifact_digest=DIGEST,
        endpoint_binding_id="endpoint",
        credential_slot_id="slot",
        credential_version="1",
        operation="realtime.open",
        payload={"model": "gpt-realtime"},
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


def test_realtime_transport_uses_one_handshake_and_one_frame_per_message():
    async def scenario():
        socket = _Socket()
        session = _Session(socket)
        lease = _Lease(session)
        transport = OpenAIRealtimeTransport(
            endpoint_id="endpoint",
            base_url="https://api.openai.com",
            connection=lease,
            auth_headers=lambda: _headers(),
        )
        lifecycle = _Lifecycle()
        opened = await transport.open_once(
            _request(),
            local_deadline=asyncio.get_running_loop().time() + 2,
            lifecycle=lifecycle,
        )
        result = await opened.connection.send_once(
            SessionApplicationMessage(
                session_id="session",
                sequence=1,
                message_type="response.create",
                payload={"response": {"modalities": ["text"]}},
            ),
            local_deadline=asyncio.get_running_loop().time() + 2,
            lifecycle=lifecycle,
        )
        inbound = [event async for event in opened.connection.inbound()]
        assert len(session.calls) == 1
        assert session.calls[0][0] == ("wss://api.openai.com/v1/realtime?model=gpt-realtime")
        assert len(socket.sent) == 1
        assert result.payload["sent"] is True
        assert inbound == [{"type": "response.done"}]
        assert (lifecycle.started, lifecycle.responded) == (2, 2)
        await transport.aclose()
        assert socket.closed and lease.released

    asyncio.run(scenario())


async def _headers():
    return {"Authorization": "Bearer secret"}
