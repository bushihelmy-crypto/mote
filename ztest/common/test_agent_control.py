#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for the zero-cycle spawn vocabulary + ambient discovery."""

import types

import pytest

from mote.contracts.spawn import Lifecycle, SpawnContext, SpawnSpec
from mote.runtime.agent.control import current_control, resolve_control, set_control, spawn_and_run
from mote.runtime.errors import AgentLimitReached


# ---------------------------------------------------------------------------
# Ambient discovery
# ---------------------------------------------------------------------------
def test_current_control_default_is_none():
    assert current_control() is None


def test_set_control_binds_and_restores():
    sentinel = object()
    assert current_control() is None
    with set_control(sentinel):
        assert current_control() is sentinel
    assert current_control() is None


def test_resolve_control_prefers_explicit_ctx():
    ambient = object()
    explicit = object()
    ctx = types.SimpleNamespace(agent_control=explicit)
    with set_control(ambient):
        assert resolve_control(ctx) is explicit


def test_resolve_control_falls_back_to_ambient():
    ambient = object()
    ctx = types.SimpleNamespace(agent_control=None)
    with set_control(ambient):
        assert resolve_control(ctx) is ambient
        assert resolve_control(None) is ambient


def test_resolve_control_none_when_unbound():
    assert resolve_control(None) is None
    assert resolve_control(types.SimpleNamespace(agent_control=None)) is None


# ---------------------------------------------------------------------------
# Spec / Lifecycle shape
# ---------------------------------------------------------------------------
def test_spawn_spec_defaults():
    spec = SpawnSpec(role_factory=lambda ctx: None)
    assert spec.lifecycle is Lifecycle.EPHEMERAL
    assert spec.cost_rollup is True
    assert spec.watch_completion is True
    assert spec.nickname is None
    assert spec.max_depth is None


def test_spawn_context_defaults():
    ctx = SpawnContext()
    assert ctx.parent_id is None
    assert ctx.parent_session_id == ""


# ---------------------------------------------------------------------------
# spawn_and_run
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_spawn_and_run_no_plane_raises():
    # Every child is born through the plane — there is no plane-less fallback.
    # A missing plane is a wiring bug, not a degrade path.
    def factory(spawn_ctx):
        raise AssertionError("factory must not run without a plane")

    spec = SpawnSpec(role_factory=factory, nickname="x")
    with pytest.raises(RuntimeError, match="requires an active control plane"):
        await spawn_and_run(spec, "msg")


class _FakeHandle:
    def __init__(self, summary, *, role=None, events=None):
        self._summary = summary
        self.closed = False
        # A minimal runtime.role surface so on_spawn / builder messages resolve.
        self.runtime = types.SimpleNamespace(role=role)
        self._events = events  # shared ordered log of lifecycle events

    async def run_to_completion(self, message):
        if self._events is not None:
            self._events.append("run")
        return self._summary

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        self.closed = True
        return False


class _FakeControl:
    def __init__(self, *, summary="planed", raise_limit=False, role=None, events=None):
        self._summary = summary
        self._raise_limit = raise_limit
        self._role = role
        self._events = events
        self.spawned = []

    async def spawn_agent(self, spec):
        self.spawned.append(spec)
        if self._raise_limit:
            raise AgentLimitReached(1)
        return _FakeHandle(self._summary, role=self._role, events=self._events)


@pytest.mark.asyncio
async def test_spawn_and_run_with_ambient_plane():
    control = _FakeControl(summary="from-plane")
    spec = SpawnSpec(role_factory=lambda c: None, nickname="x")
    with set_control(control):
        out = await spawn_and_run(spec, "msg")
    assert out == "from-plane"
    assert len(control.spawned) == 1


@pytest.mark.asyncio
async def test_spawn_and_run_with_explicit_ctx_plane():
    control = _FakeControl(summary="explicit")
    ctx = types.SimpleNamespace(agent_control=control)
    spec = SpawnSpec(role_factory=lambda c: None, nickname="x")
    out = await spawn_and_run(spec, "msg", ctx=ctx)
    assert out == "explicit"


@pytest.mark.asyncio
async def test_spawn_and_run_cap_returns_none():
    control = _FakeControl(raise_limit=True)
    spec = SpawnSpec(role_factory=lambda c: None, nickname="x")
    with set_control(control):
        out = await spawn_and_run(spec, "msg")
    assert out is None


# ---------------------------------------------------------------------------
# on_spawn seed window
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_on_spawn_runs_on_role_before_first_turn():
    # The hook must run on the built role AFTER it exists but BEFORE run.
    events: list[str] = []
    role = object()
    control = _FakeControl(summary="ok", role=role, events=events)
    seen = {}

    async def on_spawn(r):
        events.append("seed")
        seen["role"] = r

    spec = SpawnSpec(role_factory=lambda c: None, nickname="x")
    with set_control(control):
        await spawn_and_run(spec, "msg", on_spawn=on_spawn)
    assert seen["role"] is role
    assert events == ["seed", "run"]  # seeded strictly before the first turn


@pytest.mark.asyncio
async def test_no_on_spawn_is_noop():
    # Absent hook: spawn_and_run behaves exactly as before.
    control = _FakeControl(summary="ok", role=object())
    spec = SpawnSpec(role_factory=lambda c: None, nickname="x")
    with set_control(control):
        out = await spawn_and_run(spec, "msg")
    assert out == "ok"
