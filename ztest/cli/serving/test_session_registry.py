#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for :class:`SessionRegistry` — the resident ``session_id → session`` map.

The registry mints sessions from a shared ``role_factory`` (the ``EngineBuild``
closure), keeps them resident across turns, resumes persisted rollouts on first
touch, and tears them down on evict. Fakes stand in for the role / control plane
so the multiplexing is testable without a real engine. ``backend`` is patched at
the module the registry imports it through.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, List, Optional

import pytest

from mote.contracts.session import SessionHostingError, SessionHostingErrorKind
from mote.product.session_hosting import registry as sr
from mote.product.session_hosting.registry import SessionRegistry
from mote.ztest.telemetry import InlineTelemetry


class FakeRole:
    def __init__(self, session_id: str, name: str = "Assistant") -> None:
        self.session_id = session_id
        self.state = SimpleNamespace(env=None)
        self.telemetry = InlineTelemetry()
        self.role_schema = SimpleNamespace(name=name)
        self.cleaned = False

    async def cleanup(self) -> None:
        self.cleaned = True


class FakeControl:
    def __init__(self, role: FakeRole) -> None:
        self.role = role
        self.started = False
        self.stopped = False

    def start(self) -> None:
        self.started = True

    async def stop(self) -> None:
        self.stopped = True


@pytest.fixture
def patched_backend(monkeypatch):
    """Patch ``backend`` at the registry module: deterministic build/resume/stop."""
    built: List[FakeRole] = []
    resumed: List[str] = []

    def build_control(role: FakeRole):
        control = FakeControl(role)
        return control, SimpleNamespace(role=role)

    def resume_role(role: FakeRole) -> bool:
        resumed.append(role.session_id)
        return True

    monkeypatch.setattr(sr, "_build_control", build_control)
    monkeypatch.setattr(sr, "_resume_role", resume_role)
    monkeypatch.setattr(sr, "_role_cleanup", lambda role: role.cleanup)
    return SimpleNamespace(built=built, resumed=resumed)


def make_factory(built: List[FakeRole]):
    """A role_factory closure that mints a FakeRole, tracking created roles."""

    def role_factory(request):
        role = FakeRole(session_id=request.session_id or f"new-{len(built)}", name=request.name)
        built.append(role)
        return role

    return role_factory


# --------------------------------------------------------------------------
# explicit create/load
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_load_existing_starts_verified_session(patched_backend):
    reg = SessionRegistry(make_factory(patched_backend.built))
    session = await reg.load_existing("s1")
    assert session.session_id == "s1"
    assert session.control.started is True  # control plane started
    assert session.agent_id == "s1"
    assert reg.get("s1") is session


@pytest.mark.asyncio
async def test_load_existing_is_idempotent_per_id(patched_backend):
    reg = SessionRegistry(make_factory(patched_backend.built))
    first = await reg.load_existing("s1")
    second = await reg.load_existing("s1")
    assert first is second  # resident across turns — same session object
    assert len(patched_backend.built) == 1  # built only once


@pytest.mark.asyncio
async def test_known_id_resumes_persisted_rollout(patched_backend):
    reg = SessionRegistry(make_factory(patched_backend.built))
    await reg.load_existing("existing-thread")
    assert patched_backend.resumed == ["existing-thread"]


@pytest.mark.asyncio
async def test_none_id_mints_fresh_without_resume(patched_backend):
    reg = SessionRegistry(make_factory(patched_backend.built))
    session = await reg.create_new()
    assert session.session_id.startswith("new-")
    assert patched_backend.resumed == []  # a brand-new thread never resumes


@pytest.mark.asyncio
async def test_unknown_load_does_not_register_empty_replacement(patched_backend, monkeypatch):
    monkeypatch.setattr(sr, "_resume_role", lambda role: False)
    reg = SessionRegistry(make_factory(patched_backend.built))
    with pytest.raises(SessionHostingError) as raised:
        await reg.load_existing("missing")
    assert raised.value.kind is SessionHostingErrorKind.NOT_FOUND
    assert reg.get("missing") is None


@pytest.mark.asyncio
async def test_corrupt_load_preserves_nonresident_state(patched_backend, monkeypatch):
    def corrupt(role):
        raise ValueError("corrupt journal")

    monkeypatch.setattr(sr, "_resume_role", corrupt)
    reg = SessionRegistry(make_factory(patched_backend.built))
    with pytest.raises(SessionHostingError) as raised:
        await reg.load_existing("broken")
    assert raised.value.kind is SessionHostingErrorKind.LOAD_FAILED
    assert reg.get("broken") is None


@pytest.mark.asyncio
async def test_fork_failure_never_creates_fresh_session(patched_backend):
    class UnforkableRole(FakeRole):
        async def fork_session(self):
            raise NotImplementedError("unsupported")

    reg = SessionRegistry(lambda request: UnforkableRole(request.session_id or "new"))
    with pytest.raises(SessionHostingError) as raised:
        await reg.fork_existing("source")
    assert raised.value.kind is SessionHostingErrorKind.FORK_UNSUPPORTED
    assert reg.session_ids == ["source"]


@pytest.mark.asyncio
async def test_distinct_ids_get_distinct_sessions(patched_backend):
    reg = SessionRegistry(make_factory(patched_backend.built))
    a = await reg.load_existing("a")
    b = await reg.load_existing("b")
    assert a is not b
    assert a.control is not b.control
    assert set(reg.session_ids) == {"a", "b"}


# --------------------------------------------------------------------------
# evict / aclose
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_evict_stops_control_and_cleans_role(patched_backend):
    reg = SessionRegistry(make_factory(patched_backend.built))
    session = await reg.load_existing("s1")
    existed = await reg.evict("s1")
    assert existed is True
    assert session.control.stopped is True
    assert session.role.cleaned is True
    assert reg.get("s1") is None


@pytest.mark.asyncio
async def test_evict_releases_engine_ownership(patched_backend):
    released: List[FakeRole] = []

    class FakeEngine:
        async def release(self, role: FakeRole) -> None:
            released.append(role)
            await role.cleanup()

        async def aclose(self) -> None:
            pass

    reg = SessionRegistry(make_factory(patched_backend.built), engine=FakeEngine())
    session = await reg.load_existing("s1")

    await reg.evict("s1")

    assert released == [session.role]
    assert session.role.cleaned is True


@pytest.mark.asyncio
async def test_evict_unknown_id_is_noop(patched_backend):
    reg = SessionRegistry(make_factory(patched_backend.built))
    assert await reg.evict("ghost") is False


@pytest.mark.asyncio
async def test_aclose_evicts_all(patched_backend):
    reg = SessionRegistry(make_factory(patched_backend.built))
    s1 = await reg.load_existing("s1")
    s2 = await reg.load_existing("s2")
    await reg.aclose()
    assert s1.control.stopped and s2.control.stopped
    assert reg.session_ids == []


@pytest.mark.asyncio
async def test_failed_evict_remains_resident_for_retry(patched_backend):
    class RetryRole(FakeRole):
        def __init__(self, session_id: str) -> None:
            super().__init__(session_id)
            self.attempts = 0

        async def cleanup(self) -> None:
            self.attempts += 1
            if self.attempts == 1:
                raise RuntimeError("transient cleanup")
            await super().cleanup()

    role = RetryRole("s1")
    reg = SessionRegistry(lambda _request: role)
    session = await reg.load_existing("s1")

    with pytest.raises(RuntimeError, match="transient cleanup"):
        await reg.evict("s1")
    assert reg.get("s1") is session

    assert await reg.evict("s1") is True
    assert reg.get("s1") is None
    assert role.cleaned is True
