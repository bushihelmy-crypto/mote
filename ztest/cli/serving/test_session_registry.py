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

from mote.cli.serving import session_registry as sr
from mote.cli.serving.session_registry import SessionRegistry


class FakeBus:
    def __init__(self) -> None:
        self.subscribed: List[Any] = []

    def subscribe(self, c: Any) -> None:
        self.subscribed.append(c)

    def unsubscribe(self, c: Any) -> None:
        if c in self.subscribed:
            self.subscribed.remove(c)


class FakeRole:
    def __init__(self, session_id: str, name: str = "Assistant") -> None:
        self.session_id = session_id
        self.state = SimpleNamespace(env=None)
        self.event_bus = FakeBus()
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

    monkeypatch.setattr(sr.backend, "build_control", build_control)
    monkeypatch.setattr(sr.backend, "resume_role", resume_role)
    monkeypatch.setattr(sr.backend, "role_session_id", lambda role: role.session_id)
    monkeypatch.setattr(sr.backend, "role_cleanup", lambda role: getattr(role, "cleanup", None))
    return SimpleNamespace(built=built, resumed=resumed)


def make_factory(built: List[FakeRole]):
    """A role_factory closure that mints a FakeRole, tracking created roles."""

    def role_factory(*, name: str = "Assistant", session_id: Optional[str] = None, agent_type=None):
        role = FakeRole(session_id=session_id or f"new-{len(built)}", name=name)
        built.append(role)
        return role

    return role_factory


# --------------------------------------------------------------------------
# get_or_create
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_or_create_mints_and_starts(patched_backend):
    reg = SessionRegistry(make_factory(patched_backend.built))
    session = await reg.get_or_create("s1")
    assert session.session_id == "s1"
    assert session.control.started is True  # control plane started
    assert session.agent_id == "s1"
    assert reg.get("s1") is session


@pytest.mark.asyncio
async def test_get_or_create_is_idempotent_per_id(patched_backend):
    reg = SessionRegistry(make_factory(patched_backend.built))
    first = await reg.get_or_create("s1")
    second = await reg.get_or_create("s1")
    assert first is second  # resident across turns — same session object
    assert len(patched_backend.built) == 1  # built only once


@pytest.mark.asyncio
async def test_known_id_resumes_persisted_rollout(patched_backend):
    reg = SessionRegistry(make_factory(patched_backend.built))
    await reg.get_or_create("existing-thread")
    assert patched_backend.resumed == ["existing-thread"]


@pytest.mark.asyncio
async def test_none_id_mints_fresh_without_resume(patched_backend):
    reg = SessionRegistry(make_factory(patched_backend.built))
    session = await reg.get_or_create(None)
    assert session.session_id.startswith("new-")
    assert patched_backend.resumed == []  # a brand-new thread never resumes


@pytest.mark.asyncio
async def test_distinct_ids_get_distinct_sessions(patched_backend):
    reg = SessionRegistry(make_factory(patched_backend.built))
    a = await reg.get_or_create("a")
    b = await reg.get_or_create("b")
    assert a is not b
    assert a.control is not b.control
    assert set(reg.session_ids) == {"a", "b"}


# --------------------------------------------------------------------------
# evict / aclose
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_evict_stops_control_and_cleans_role(patched_backend):
    reg = SessionRegistry(make_factory(patched_backend.built))
    session = await reg.get_or_create("s1")
    existed = await reg.evict("s1")
    assert existed is True
    assert session.control.stopped is True
    assert session.role.cleaned is True
    assert reg.get("s1") is None


@pytest.mark.asyncio
async def test_evict_unknown_id_is_noop(patched_backend):
    reg = SessionRegistry(make_factory(patched_backend.built))
    assert await reg.evict("ghost") is False


@pytest.mark.asyncio
async def test_aclose_evicts_all(patched_backend):
    reg = SessionRegistry(make_factory(patched_backend.built))
    s1 = await reg.get_or_create("s1")
    s2 = await reg.get_or_create("s2")
    await reg.aclose()
    assert s1.control.stopped and s2.control.stopped
    assert reg.session_ids == []
