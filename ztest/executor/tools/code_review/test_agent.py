#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for the code-review child-agent plumbing (``_agent.py``).

Focuses on the spawn-plane routing added in the unified-lifecycle migration:
``run_child`` funnels every leaf through ``spawn_and_run`` (so cap / lineage
apply), degrades to ``None`` only on a cap-refused spawn (AgentLimitReached),
and lets structural failures propagate loudly. ``run_child_for_text`` stays a
thin shim over it for the three pipeline callers.
"""
from __future__ import annotations

import types

import pytest
from mote.common.agent_control import set_control
from mote.common.exception import AgentLimitReached
from mote.executor.tools.code_review._agent import run_child, run_child_for_text


class _LeafRole:
    """Minimal duck-typed Role for the spawn-plane tests."""

    def __init__(self, summary: str = "leaf"):
        self.state = types.SimpleNamespace(last_end_output=summary)
        self.ran = None
        self.cleaned = False

    async def run(self, with_message=None):
        self.ran = with_message

    async def cleanup(self):
        self.cleaned = True


# ---------------------------------------------------------------------------
# No plane bound is a wiring bug: run_child lets the RuntimeError surface loudly
# (it no longer swallows structural failures into a fake-clean None).
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_run_child_no_plane_raises():
    leaf = _LeafRole(summary="done")
    with pytest.raises(RuntimeError):
        await run_child(lambda _ctx: leaf, "prompt", label="plan")


# ---------------------------------------------------------------------------
# run_child_for_text stays a thin shim that routes a prebuilt role through the
# plane (factory simply returns the already-built role).
# ---------------------------------------------------------------------------
class _ShimHandle:
    def __init__(self, role):
        self.runtime = types.SimpleNamespace(role=role)
        self._role = role

    async def run_to_completion(self, message):
        await self._role.run(with_message=message)
        return self._role.state.last_end_output

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _ShimControl:
    async def spawn_agent(self, spec):
        return _ShimHandle(spec.role_factory(None))


@pytest.mark.asyncio
async def test_run_child_for_text_shim_runs_prebuilt_role():
    leaf = _LeafRole(summary="critique")
    with set_control(_ShimControl()):
        out = await run_child_for_text(leaf, "review this", label="review_filter")
    assert out == "critique"
    assert leaf.ran.content == "review this"


# ---------------------------------------------------------------------------
# Cap enforcement via the plane: bulk leaves past the cap degrade to None
# ---------------------------------------------------------------------------
class _CapHandle:
    def __init__(self):
        self.runtime = types.SimpleNamespace(role=_LeafRole())

    async def run_to_completion(self, message):
        return "spawned"

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _CapControl:
    """Allows ``cap`` concurrent spawns, then raises AgentLimitReached."""

    def __init__(self, cap: int):
        self._cap = cap
        self.spawned = 0

    async def spawn_agent(self, spec):
        if self.spawned >= self._cap:
            raise AgentLimitReached(self._cap)
        self.spawned += 1
        return _CapHandle()


@pytest.mark.asyncio
async def test_run_child_cap_second_leaf_degrades_to_none():
    control = _CapControl(cap=1)
    with set_control(control):
        first = await run_child(lambda _c: _LeafRole(), "a", label="review a.py")
        second = await run_child(lambda _c: _LeafRole(), "b", label="review b.py")
    assert first == "spawned"
    assert second is None
    assert control.spawned == 1


# ---------------------------------------------------------------------------
# A structural run failure propagates: run_child does NOT swallow it (per-file
# isolation lives in the batch node's _safe_review, not here).
# ---------------------------------------------------------------------------
class _BoomControl:
    async def spawn_agent(self, spec):
        raise RuntimeError("boom")


@pytest.mark.asyncio
async def test_run_child_run_failure_propagates():
    with set_control(_BoomControl()):
        with pytest.raises(RuntimeError):
            await run_child(lambda _c: _LeafRole(), "x", label="plan")
