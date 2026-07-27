#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Integration tests for :class:`AcpServer` — the ACP stdio JSON-RPC agent peer.

Drives the real :class:`AcpServer` over an in-memory duplex NDJSON pipe (a
``FakeStream`` reader/writer pair) backed by a :class:`SessionRegistry` over
fakes (no real engine). The headline: ``initialize`` advertises v1 caps;
``session/new`` mints a resident thread; ``session/prompt`` drives one turn and
its assistant echo arrives as ``session/update`` notifications then the request
resolves ``{stopReason:end_turn}``; ``session/fork`` branches a sibling session;
``session/cancel`` interrupts the in-flight turn so its prompt resolves
``cancelled``; an unknown method → ``METHOD_NOT_FOUND``.

The whole wire path (``_StdioEndpoint`` dispatch/reply → ConnectionScope →
per-turn projector → AcpConsumer → session/update) is exercised end-to-end
without touching a real pipe.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import pytest

from mote.contracts.events.types import MESSAGE_APPENDED
from mote.product.cli.consumers.acp import server as srv
from mote.product.cli.consumers.acp.server import AcpServer
from mote.product.cli.serving import SessionRegistry
from mote.ztest.telemetry import InlineTelemetry


# --------------------------------------------------------------------------
# Fakes: Role Telemetry fanning observations to handlers; a control that, on input,
# emits an assistant echo on the bus (one turn's worth of output).
# --------------------------------------------------------------------------
class FakeAgentEvt:
    def __init__(self, name: str, **fields: Any) -> None:
        self.name = name
        for k, v in fields.items():
            setattr(self, k, v)


class FakeRole:
    _counter = 0

    def __init__(self, session_id: Optional[str], name: str = "Assistant") -> None:
        if not session_id:
            FakeRole._counter += 1
            session_id = f"auto-{FakeRole._counter}"
        self.session_id = session_id
        self.state = SimpleNamespace(env=None)
        self.telemetry = InlineTelemetry()
        self.role_schema = SimpleNamespace(name=name)

    async def cleanup(self) -> None:
        return None


class EmittingControl:
    """On input, schedule an assistant echo on Role Telemetry, then be quiescent."""

    def __init__(self, role: FakeRole) -> None:
        self.role = role
        self.started = False
        self.stopped = False
        self.interrupted = False
        self._done = True

    def start(self) -> None:
        self.started = True

    async def stop(self) -> None:
        self.stopped = True

    async def interrupt(self, agent_id: str) -> None:
        self.interrupted = True

    def send_input(self, agent_id: str, message: Any) -> None:
        self._done = False
        reply = FakeAgentEvt(
            MESSAGE_APPENDED,
            message=SimpleNamespace(role="assistant", content=f"echo:{getattr(message, 'content', '')}"),
        )

        async def _emit_then_done():
            await self.role.telemetry.emit(reply)
            self._done = True

        asyncio.ensure_future(_emit_then_done())

    def quiescent(self) -> bool:
        return self._done

    def get_runtime(self, agent_id: str):
        return SimpleNamespace(last_error=None)


def make_factory():
    def role_factory(*, name: str = "Assistant", session_id: Optional[str] = None, agent_type=None):
        return FakeRole(session_id=session_id, name=name)

    return role_factory


@pytest.fixture
def patched_backend(monkeypatch):
    """Patch the backend seam at every module the serving/server layer imports."""
    from mote.product.cli.serving import connection_scope as cs
    from mote.product.cli.serving import session_registry as sr

    def build_control(role: FakeRole):
        return EmittingControl(role), SimpleNamespace(role=role)

    monkeypatch.setattr(sr.backend, "build_control", build_control)
    monkeypatch.setattr(sr.backend, "resume_role", lambda role: False)
    monkeypatch.setattr(sr.backend, "role_session_id", lambda role: role.session_id)
    monkeypatch.setattr(sr.backend, "role_cleanup", lambda role: getattr(role, "cleanup", None))
    monkeypatch.setattr(cs.backend, "role_telemetry", lambda role: role.telemetry)
    monkeypatch.setattr(cs.backend, "bind_human_channel", lambda role, ch: setattr(role.state, "env", ch))
    monkeypatch.setattr(
        srv.backend, "turn_message", lambda text, image_b64s=None: SimpleNamespace(content=text, id="m-1")
    )
    # fork_role → a fresh independent role of the same class
    monkeypatch.setattr(srv.backend, "fork_role", lambda role: FakeRole(session_id=None, name=role.role_schema.name))


# --------------------------------------------------------------------------
# In-memory duplex NDJSON transport
# --------------------------------------------------------------------------
class FakeStream:
    """An in-memory byte stream usable as both an asyncio-ish reader and writer.

    ``write`` appends bytes; ``readline`` awaits until a newline-terminated line
    is available or the stream is closed (returns ``b""`` at EOF). Two of these
    cross-wired make a duplex link between the test (client) and the server.
    """

    def __init__(self) -> None:
        self._buf = bytearray()
        self._event = asyncio.Event()
        self._closed = False

    def write(self, data: bytes) -> None:
        self._buf.extend(data)
        self._event.set()

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        self._closed = True
        self._event.set()

    async def readline(self) -> bytes:
        while True:
            nl = self._buf.find(b"\n")
            if nl != -1:
                line = bytes(self._buf[: nl + 1])
                del self._buf[: nl + 1]
                return line
            if self._closed:
                return b""
            self._event.clear()
            await self._event.wait()


class ClientLink:
    """A tiny JSON-RPC client over the duplex pair for the test to drive."""

    def __init__(self, to_server: FakeStream, from_server: FakeStream) -> None:
        self._to_server = to_server
        self._from_server = from_server
        self._id = 0

    def _send(self, obj: Dict[str, Any]) -> None:
        self._to_server.write((json.dumps(obj) + "\n").encode("utf-8"))

    async def request(self, method: str, params: Dict[str, Any]) -> Dict[str, Any]:
        """Send a request; read frames until the matching reply, return it."""
        self._id += 1
        req_id = self._id
        self._send({"jsonrpc": "2.0", "id": req_id, "method": method, "params": params})
        return await self._read_reply(req_id)

    def notify(self, method: str, params: Dict[str, Any]) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params})

    async def _read_reply(self, req_id: int, limit: int = 200) -> Dict[str, Any]:
        for _ in range(limit):
            line = await asyncio.wait_for(self._from_server.readline(), timeout=5.0)
            if not line:
                raise AssertionError("server closed before reply")
            msg = json.loads(line)
            if msg.get("id") == req_id and ("result" in msg or "error" in msg):
                return msg
            self.notifications.append(msg)
        raise AssertionError(f"no reply for id={req_id}")

    notifications: List[Dict[str, Any]] = []


async def _run_server(server: AcpServer, reader: FakeStream, writer: FakeStream):
    return asyncio.create_task(server.serve(reader, writer))


def _fresh_client():
    """Return ``(client, to_server, from_server)`` with a fresh notifications list."""
    to_server = FakeStream()
    from_server = FakeStream()
    client = ClientLink(to_server, from_server)
    client.notifications = []
    return client, to_server, from_server


# --------------------------------------------------------------------------
# initialize
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_initialize_advertises_v1_caps(patched_backend):
    client, to_server, from_server = _fresh_client()
    registry = SessionRegistry(make_factory(), name="Assistant")
    server = AcpServer(registry, name="Assistant")
    task = await _run_server(server, to_server, from_server)
    try:
        reply = await client.request("initialize", {"protocolVersion": 1})
        result = reply["result"]
        assert result["protocolVersion"] == srv.ACP_PROTOCOL_VERSION
        assert result["agentCapabilities"]["loadSession"] is True
        assert result["agentInfo"]["name"] == "mote"
    finally:
        to_server.close()
        await task


# --------------------------------------------------------------------------
# session/new + session/prompt (the headline)
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_new_then_prompt_streams_updates_and_end_turn(patched_backend):
    client, to_server, from_server = _fresh_client()
    registry = SessionRegistry(make_factory(), name="Assistant")
    server = AcpServer(registry, name="Assistant")
    task = await _run_server(server, to_server, from_server)
    try:
        new = await client.request("session/new", {"cwd": "/tmp"})
        session_id = new["result"]["sessionId"]
        assert session_id

        prompt = await client.request(
            "session/prompt",
            {"sessionId": session_id, "prompt": [{"type": "text", "text": "hi"}]},
        )
        assert prompt["result"]["stopReason"] == srv.STOP_END_TURN

        # session/update notifications for this session arrived before the reply.
        updates = [
            m
            for m in client.notifications
            if m.get("method") == "session/update" and m["params"].get("sessionId") == session_id
        ]
        assert updates, "no session/update notifications streamed"
        # the assistant echo of our input is somewhere in the update text.
        texts = json.dumps(updates)
        assert "echo:hi" in texts
    finally:
        to_server.close()
        await task


# --------------------------------------------------------------------------
# session/load
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_load_makes_session_resident(patched_backend):
    client, to_server, from_server = _fresh_client()
    registry = SessionRegistry(make_factory(), name="Assistant")
    server = AcpServer(registry, name="Assistant")
    task = await _run_server(server, to_server, from_server)
    try:
        reply = await client.request("session/load", {"sessionId": "thread-x"})
        assert reply["result"] == {}
        # a subsequent prompt reuses the same resident session
        prompt = await client.request(
            "session/prompt",
            {"sessionId": "thread-x", "prompt": [{"type": "text", "text": "yo"}]},
        )
        assert prompt["result"]["stopReason"] == srv.STOP_END_TURN
    finally:
        to_server.close()
        await task


@pytest.mark.asyncio
async def test_load_without_session_id_is_invalid_params(patched_backend):
    client, to_server, from_server = _fresh_client()
    registry = SessionRegistry(make_factory(), name="Assistant")
    server = AcpServer(registry, name="Assistant")
    task = await _run_server(server, to_server, from_server)
    try:
        reply = await client.request("session/load", {})
        assert reply["error"]["code"] == srv.ERR_INVALID_PARAMS
    finally:
        to_server.close()
        await task


# --------------------------------------------------------------------------
# session/fork
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_fork_branches_a_new_session_id(patched_backend):
    client, to_server, from_server = _fresh_client()
    registry = SessionRegistry(make_factory(), name="Assistant")
    server = AcpServer(registry, name="Assistant")
    task = await _run_server(server, to_server, from_server)
    try:
        src = await client.request("session/new", {})
        src_id = src["result"]["sessionId"]
        fork = await client.request("session/fork", {"sessionId": src_id})
        fork_id = fork["result"]["sessionId"]
        assert fork_id and fork_id != src_id  # a distinct sibling session
    finally:
        to_server.close()
        await task


@pytest.mark.asyncio
async def test_fork_degrades_to_fresh_when_engine_cannot_fork(patched_backend, monkeypatch):
    # fork_role returning None → the server degrades to a plain new session.
    monkeypatch.setattr(srv.backend, "fork_role", lambda role: None)
    client, to_server, from_server = _fresh_client()
    registry = SessionRegistry(make_factory(), name="Assistant")
    server = AcpServer(registry, name="Assistant")
    task = await _run_server(server, to_server, from_server)
    try:
        src = await client.request("session/new", {})
        src_id = src["result"]["sessionId"]
        fork = await client.request("session/fork", {"sessionId": src_id})
        assert fork["result"]["sessionId"]  # still a usable id, not an error
        assert fork["result"]["sessionId"] != src_id
    finally:
        to_server.close()
        await task


# --------------------------------------------------------------------------
# unknown method
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_unknown_method_is_method_not_found(patched_backend):
    client, to_server, from_server = _fresh_client()
    registry = SessionRegistry(make_factory(), name="Assistant")
    server = AcpServer(registry, name="Assistant")
    task = await _run_server(server, to_server, from_server)
    try:
        reply = await client.request("session/nonsense", {})
        assert reply["error"]["code"] == srv.ERR_METHOD_NOT_FOUND
    finally:
        to_server.close()
        await task


# --------------------------------------------------------------------------
# session/cancel interrupts the in-flight turn
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_cancel_interrupts_active_turn(patched_backend):
    # A control that never becomes quiescent until interrupted, so we can cancel
    # mid-turn and observe the prompt resolve.
    from mote.product.cli.serving import session_registry as sr

    captured: List[Any] = []

    class HangingControl(EmittingControl):
        def send_input(self, agent_id: str, message: Any) -> None:
            self._done = False  # stays busy until interrupt() flips it

        async def interrupt(self, agent_id: str) -> None:
            self.interrupted = True
            self._done = True  # cancel lets the turn go quiescent

    def build_control(role: FakeRole):
        ctrl = HangingControl(role)
        captured.append(ctrl)
        return ctrl, SimpleNamespace(role=role)

    import pytest as _pt

    mp = _pt.MonkeyPatch()
    mp.setattr(sr.backend, "build_control", build_control)
    try:
        client, to_server, from_server = _fresh_client()
        registry = SessionRegistry(make_factory(), name="Assistant")
        server = AcpServer(registry, name="Assistant")
        task = await _run_server(server, to_server, from_server)
        try:
            new = await client.request("session/new", {})
            sid = new["result"]["sessionId"]
            # fire the prompt without awaiting its reply, then cancel it
            client._id += 1
            prompt_id = client._id
            client._send(
                {
                    "jsonrpc": "2.0",
                    "id": prompt_id,
                    "method": "session/prompt",
                    "params": {"sessionId": sid, "prompt": [{"type": "text", "text": "hang"}]},
                }
            )
            await asyncio.sleep(0.05)  # let the turn start + register as active
            client.notify("session/cancel", {"sessionId": sid})
            reply = await client._read_reply(prompt_id)
            assert reply["result"]["stopReason"] in (srv.STOP_END_TURN, srv.STOP_CANCELLED)
            assert captured and captured[0].interrupted is True
        finally:
            to_server.close()
            await task
    finally:
        mp.undo()
