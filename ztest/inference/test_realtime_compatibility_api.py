import asyncio
from collections import deque

from aiohttp import WSMsgType
from aiohttp.test_utils import TestClient, TestServer

from mote.contracts.inference.events import SessionEventType, SessionLifecycleEvent
from mote.product.interfaces.inference_api import build_inference_api


class _Gateway:
    def route_profiles(self, route):
        return ()


class _Session:
    def __init__(self):
        self.events = asyncio.Queue()
        self.sent = []
        self.closed = []
        self.events.put_nowait(
            SessionLifecycleEvent(
                session_id="session-1",
                sequence=1,
                receipt_revision=4,
                generation_id="generation-1",
                event_type=SessionEventType.OPENED,
                payload={"model": "realtime"},
            )
        )

    def __aiter__(self):
        return self

    async def __anext__(self):
        return await self.events.get()

    async def send(self, *, sequence, message_type, payload):
        self.sent.append((sequence, message_type, payload))
        await self.events.put(
            SessionLifecycleEvent(
                session_id="session-1",
                sequence=2,
                receipt_revision=7,
                generation_id="generation-1",
                event_type=SessionEventType.MESSAGE_SENT,
                payload={"application_sequence": sequence},
            )
        )

    async def close(self, reason):
        self.closed.append(reason)


class _Owner:
    def __init__(self):
        self.payloads = []
        self.session = _Session()

    async def open(self, payload):
        self.payloads.append(payload)
        return self.session


class _FailedSession(_Session):
    def __init__(self):
        self.events = deque(
            [
                SessionLifecycleEvent(
                    session_id="session-1",
                    sequence=1,
                    receipt_revision=4,
                    generation_id="generation-1",
                    event_type=SessionEventType.IN_DOUBT,
                    payload={"reason": "provider stream failed"},
                )
            ]
        )
        self.sent = []
        self.closed = []

    async def __anext__(self):
        if not self.events:
            raise StopAsyncIteration
        return self.events.popleft()


class _FailedOwner(_Owner):
    def __init__(self):
        self.payloads = []
        self.session = _FailedSession()


def test_realtime_websocket_projects_one_session_owner():
    async def scenario():
        owner = _Owner()
        app = build_inference_api(_Gateway(), bearer_token="secret", realtime_sessions=owner)
        async with TestClient(TestServer(app)) as client:
            socket = await client.ws_connect(
                "/v1/realtime?model=realtime",
                headers={"Authorization": "Bearer secret"},
            )
            opened = await socket.receive_json()
            await socket.send_json({"type": "response.create", "sequence": 1, "response": {}})
            sent = await socket.receive_json()
            await socket.close()
        return owner, opened, sent

    owner, opened, sent = asyncio.run(scenario())
    assert owner.payloads == [{"model": "realtime"}]
    assert opened["type"] == "opened"
    assert opened["receipt_revision"] == 4
    assert sent["type"] == "message_sent"
    assert owner.session.sent == [(1, "response.create", {"response": {}})]
    assert owner.session.closed == ["client disconnected"]


def test_realtime_rejects_unauthorized_and_missing_owner_before_upgrade():
    async def scenario():
        app = build_inference_api(_Gateway(), bearer_token="secret")
        async with TestClient(TestServer(app)) as client:
            unauthorized = await client.get("/v1/realtime?model=realtime")
            unavailable = await client.get(
                "/v1/realtime?model=realtime",
                headers={"Authorization": "Bearer secret"},
            )
            return unauthorized.status, unavailable.status

    assert asyncio.run(scenario()) == (401, 503)


def test_realtime_terminal_event_closes_idle_websocket():
    async def scenario():
        owner = _FailedOwner()
        app = build_inference_api(_Gateway(), bearer_token="secret", realtime_sessions=owner)
        async with TestClient(TestServer(app)) as client:
            socket = await client.ws_connect(
                "/v1/realtime?model=realtime",
                headers={"Authorization": "Bearer secret"},
            )
            terminal = await socket.receive_json()
            closed = await asyncio.wait_for(socket.receive(), timeout=1)
        return owner, terminal, closed.type

    owner, terminal, closed_type = asyncio.run(scenario())
    assert terminal["type"] == "in_doubt"
    assert closed_type in {WSMsgType.CLOSE, WSMsgType.CLOSED}
    assert owner.session.closed == ["client disconnected"]
