#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Integration tests for the AG-UI ``aiohttp`` server (Phase 2 read-only stream).

Drives the real ``create_app`` over an injected :class:`SessionRegistry` backed
by fakes (no real engine), through an ``aiohttp`` test client. The headline:
``POST /agent/{id}/run`` returns an SSE stream whose frame sequence is
``RUN_STARTED → TEXT_MESSAGE_* → RUN_FINISHED``, and the whole wire path
(ConnectionScope → per-request projector → AguiConsumer → SSE) is exercised
end-to-end. Auth, ``/info`` discovery, and ``/stop`` eviction are covered too.

The fake control emits an assistant ``MESSAGE_APPENDED`` on the session's role
bus when it receives the turn input (mirroring how a real turn fans events onto
the bus), so the projector folds a real ViewEvent into real AG-UI frames.
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any, List, Optional

import pytest
from aiohttp.test_utils import TestClient, TestServer

from mote.contracts.events.types import MESSAGE_APPENDED
from mote.product.cli.consumers.agui.server import create_app
from mote.ztest.telemetry import InlineTelemetry


# --------------------------------------------------------------------------
# Fakes: Role telemetry plus a control that emits one assistant message.
# --------------------------------------------------------------------------
class FakeAgentEvt:
    def __init__(self, name: str, **fields: Any) -> None:
        self.name = name
        for k, v in fields.items():
            setattr(self, k, v)


class FakeRole:
    def __init__(self, session_id: str, name: str = "Assistant") -> None:
        self.session_id = session_id
        self.state = SimpleNamespace(env=None)
        self.telemetry = InlineTelemetry()
        self.role_schema = SimpleNamespace(name=name)

    async def cleanup(self) -> None:
        return None


class FakeControl:
    """On input, synchronously emit an assistant reply on Role Telemetry, then be
    quiescent — one request drives exactly one turn's worth of events."""

    def __init__(self, role: FakeRole) -> None:
        self.role = role
        self.started = False
        self.stopped = False
        self._pending_reply: Optional[str] = None

    def start(self) -> None:
        self.started = True

    async def stop(self) -> None:
        self.stopped = True

    def send_input(self, agent_id: str, message: Any) -> None:
        # Echo an assistant reply derived from the user's text.
        self._pending_reply = f"echo:{getattr(message, 'content', '')}"

    def quiescent(self) -> bool:
        return True

    def get_runtime(self, agent_id: str):
        return SimpleNamespace(last_error=None)


def make_factory():
    def role_factory(*, name: str = "Assistant", session_id: Optional[str] = None, agent_type=None):
        return FakeRole(session_id=session_id or "auto-thread", name=name)

    return role_factory


@pytest.fixture
def patched_backend(monkeypatch):
    """Patch ``backend`` at BOTH modules the serving layer imports it through."""
    from mote.product.cli.consumers.agui import server as srv
    from mote.product.cli.serving import session_registry as sr

    def build_control(role: FakeRole):
        return FakeControl(role), SimpleNamespace(role=role)

    monkeypatch.setattr(sr.backend, "build_control", build_control)
    monkeypatch.setattr(sr.backend, "resume_role", lambda role: False)
    monkeypatch.setattr(sr.backend, "role_session_id", lambda role: role.session_id)
    monkeypatch.setattr(sr.backend, "role_cleanup", lambda role: getattr(role, "cleanup", None))
    # The scope + server read the bus / build the turn message via backend too.
    monkeypatch.setattr(srv.backend, "role_telemetry", lambda role: role.telemetry, raising=False)
    monkeypatch.setattr(
        srv.backend, "turn_message", lambda text, image_b64s=None: SimpleNamespace(content=text, id="m-1")
    )
    # connection_scope reads the bus through its own backend import.
    from mote.product.cli.serving import connection_scope as cs

    monkeypatch.setattr(cs.backend, "role_telemetry", lambda role: role.telemetry)


def _parse_sse(text: str) -> List[dict]:
    """Parse concatenated ``data: <json>\\n\\n`` frames into event dicts."""
    out: List[dict] = []
    for chunk in text.split("\n\n"):
        chunk = chunk.strip()
        if chunk.startswith("data:"):
            out.append(json.loads(chunk[len("data:") :].strip()))
    return out


# --------------------------------------------------------------------------
# The fake control needs to actually emit on the bus during the turn. We make
# send_input schedule the emit and quiescent() drain it. Simplest: emit inside
# send_input via the role's bus (sync helper run by the scope's await sleep(0)).
# --------------------------------------------------------------------------
class EmittingControl(FakeControl):
    def __init__(self, role: FakeRole) -> None:
        super().__init__(role)
        self._emitted = False

    def send_input(self, agent_id: str, message: Any) -> None:
        super().send_input(agent_id, message)
        # Emit synchronously-scheduled: stash the coroutine on the role so the
        # scope's poll loop drives it. Simpler: emit right away via ensure_future.
        import asyncio

        reply = FakeAgentEvt(
            MESSAGE_APPENDED,
            message=SimpleNamespace(role="assistant", content=self._pending_reply or ""),
        )
        asyncio.ensure_future(self.role.telemetry.emit(reply))


@pytest.fixture
def emitting_backend(monkeypatch, patched_backend):
    from mote.product.cli.serving import session_registry as sr

    def build_control(role: FakeRole):
        return EmittingControl(role), SimpleNamespace(role=role)

    monkeypatch.setattr(sr.backend, "build_control", build_control)


@asynccontextmanager
async def _client(**app_kwargs):
    """Build the app + a started aiohttp TestClient (manual — no aiohttp plugin).

    We construct ``TestServer``/``TestClient`` directly rather than rely on the
    ``aiohttp_client`` fixture so the test runs under pytest-asyncio's own event
    loop (the two plugins otherwise fight over loop ownership). Closes on exit.
    """
    app = create_app(make_factory(), **app_kwargs)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        yield client
    finally:
        await client.close()


# --------------------------------------------------------------------------
# /info discovery
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_info_lists_agent_and_caps(patched_backend):
    async with _client(insecure=True) as client:
        resp = await client.get("/info")
        assert resp.status == 200
        body = await resp.json()
        assert body["name"] == "mote"
        assert body["capabilities"]["streaming"] is True


# --------------------------------------------------------------------------
# Auth
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_missing_token_rejected(patched_backend):
    async with _client(token="secret") as client:
        resp = await client.get("/info")
        assert resp.status == 401


@pytest.mark.asyncio
async def test_valid_token_accepted(patched_backend):
    async with _client(token="secret") as client:
        resp = await client.get("/info", headers={"Authorization": "Bearer secret"})
        assert resp.status == 200


def test_create_app_requires_token_or_insecure():
    with pytest.raises(ValueError):
        create_app(make_factory())  # neither token nor insecure → refuse


# --------------------------------------------------------------------------
# /run SSE stream — the headline
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_run_streams_run_started_text_run_finished(emitting_backend):
    async with _client(insecure=True) as client:
        resp = await client.post(
            "/agent/main/run",
            json={"threadId": "thread-1", "messages": [{"role": "user", "content": "hi"}]},
        )
        assert resp.status == 200
        assert resp.headers["Content-Type"].startswith("text/event-stream")
        text = await resp.text()
    events = _parse_sse(text)
    types = [e["type"] for e in events]
    assert types[0] == "RUN_STARTED"
    assert types[-1] == "RUN_FINISHED"
    # The user turn is surfaced, then the assistant echo folded from the bus.
    assert "TEXT_MESSAGE_START" in types
    assert "TEXT_MESSAGE_END" in types
    # RUN_STARTED carries the thread + run correlation.
    assert events[0]["threadId"] == "thread-1"
    # An assistant echo of our input made it onto the wire.
    contents = [e.get("delta", "") for e in events if e["type"] == "TEXT_MESSAGE_CONTENT"]
    assert any("echo:hi" in c for c in contents)


@pytest.mark.asyncio
async def test_run_mints_thread_when_absent(emitting_backend):
    async with _client(insecure=True) as client:
        resp = await client.post("/agent/main/run", json={"message": "no thread id"})
        assert resp.status == 200
        events = _parse_sse(await resp.text())
    assert events[0]["type"] == "RUN_STARTED"
    assert events[0]["threadId"]  # a fresh thread id was minted


# --------------------------------------------------------------------------
# Residency + /stop
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_connect_makes_thread_resident(patched_backend):
    async with _client(insecure=True) as client:
        resp = await client.post("/connect", json={"threadId": "thread-x"})
        assert resp.status == 200
        body = await resp.json()
        assert body["threadId"] == "thread-x"
        # /info now lists it as active.
        info = await (await client.get("/info")).json()
        assert "thread-x" in info["activeThreads"]


@pytest.mark.asyncio
async def test_stop_evicts_thread(patched_backend):
    async with _client(insecure=True) as client:
        await client.post("/connect", json={"threadId": "thread-y"})
        resp = await client.post("/stop/thread-y")
        assert resp.status == 200
        assert (await resp.json())["stopped"] is True
        info = await (await client.get("/info")).json()
        assert "thread-y" not in info["activeThreads"]


# --------------------------------------------------------------------------
# Phase 3: HITL approval round-trip over /run (SSE prompt) + /respond (reply)
# --------------------------------------------------------------------------
class ApprovalControl(FakeControl):
    """On input, raise an approval up-flow through the bound human channel.

    Mirrors a real gated tool call: the turn blocks on ``env.request_approval``
    (→ ``AguiPort.decide_approval`` → SSE ``approval`` frame + broker future),
    and only becomes quiescent once the separate ``POST /respond`` resolves it.
    The mapped :data:`ApprovalChoice` is recorded so the test can assert the
    reply reached the engine.
    """

    def __init__(self, role: FakeRole) -> None:
        super().__init__(role)
        self.recorded_choice: Optional[str] = None
        self._done = False

    def send_input(self, agent_id: str, message: Any) -> None:
        import asyncio

        self._done = False
        self.recorded_choice = None
        asyncio.ensure_future(self._raise_approval())

    async def _raise_approval(self) -> None:
        from mote.contracts.permissions import ApprovalRequest

        req = ApprovalRequest(tool_name="Bash", target="rm -rf /", risk="high")
        try:
            self.recorded_choice = await self.role.state.env.request_approval(req)
        finally:
            self._done = True

    def quiescent(self) -> bool:
        return self._done


@pytest.fixture
def approval_backend(monkeypatch, patched_backend):
    from mote.product.cli.serving import session_registry as sr

    # Stash the live control so the test can read its recorded choice.
    controls: List[ApprovalControl] = []

    def build_control(role: FakeRole):
        ctrl = ApprovalControl(role)
        controls.append(ctrl)
        return ctrl, SimpleNamespace(role=role)

    monkeypatch.setattr(sr.backend, "build_control", build_control)
    return controls


async def _read_frame(content) -> Optional[dict]:
    """Read one ``data: <json>\\n\\n`` SSE frame off a streaming reader."""
    raw = await content.readuntil(b"\n\n")
    text = raw.decode("utf-8").strip()
    if text.startswith("data:"):
        return json.loads(text[len("data:") :].strip())
    return None


async def _read_until_approval(content, limit: int = 12) -> dict:
    """Read frames until the ``CUSTOM{name:'approval'}`` prompt appears."""
    for _ in range(limit):
        frame = await _read_frame(content)
        if frame and frame.get("type") == "CUSTOM" and frame.get("name") == "approval":
            return frame
    raise AssertionError("approval frame never arrived on the SSE stream")


@pytest.mark.asyncio
async def test_approval_round_trip_accept(approval_backend):
    async with _client(insecure=True) as client:
        # /run streams; the response returns once headers are sent (prepared),
        # then we read frames live while the turn blocks on the approval.
        resp = await client.post(
            "/agent/main/run",
            json={"threadId": "thread-a", "messages": [{"role": "user", "content": "go"}]},
        )
        assert resp.status == 200
        approval = await _read_until_approval(resp.content)
        pid = approval["value"]["approvalId"]
        assert pid  # a minted correlation id
        assert approval["value"]["risk"] == "high"

        # Answer on the back-channel — the blocked turn unblocks.
        reply = await client.post("/respond", json={"promptId": pid, "outcome": "accept"})
        assert reply.status == 200
        assert (await reply.json())["resolved"] is True

        # Drain the rest of the stream (RUN_FINISHED closes it).
        tail = await resp.content.read()
    types = [e["type"] for e in _parse_sse(tail.decode("utf-8"))]
    assert "RUN_FINISHED" in types
    # accept → allow_once reached the engine.
    assert approval_backend[0].recorded_choice == "allow_once"


@pytest.mark.asyncio
async def test_approval_round_trip_reject(approval_backend):
    async with _client(insecure=True) as client:
        resp = await client.post(
            "/agent/main/run",
            json={"threadId": "thread-r", "messages": [{"role": "user", "content": "go"}]},
        )
        approval = await _read_until_approval(resp.content)
        pid = approval["value"]["approvalId"]
        await client.post("/respond", json={"promptId": pid, "outcome": "reject"})
        await resp.content.read()
    assert approval_backend[0].recorded_choice == "deny"


@pytest.mark.asyncio
async def test_respond_requires_prompt_id(approval_backend):
    async with _client(insecure=True) as client:
        resp = await client.post("/respond", json={"outcome": "accept"})
        assert resp.status == 400


@pytest.mark.asyncio
async def test_respond_unknown_prompt_is_not_resolved(approval_backend):
    async with _client(insecure=True) as client:
        resp = await client.post("/respond", json={"promptId": "nope", "outcome": "accept"})
        assert resp.status == 200
        assert (await resp.json())["resolved"] is False
