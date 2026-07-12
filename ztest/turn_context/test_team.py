#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for TeamContextSource (the multi-agent lineage per-turn reminder).

The source surfaces the agent's immediate neighbourhood in the session tree —
parent, siblings, direct children — each with its session id, read live from the
control plane's registry (entirely duck-typed). It is **persisted + incremental**
like the tool-catalogue feed: the first turn emits the full roster, later turns
emit only newly-appeared teammates (tracked by ``_sent_ids``), and a
``PostCompactEvent`` resets the frontier so the next turn re-sends everything.
When no plane is bound it self-suppresses (returns None).
"""
from __future__ import annotations

import asyncio
from typing import List, Optional

from mote.common.agent_control import set_control
from mote.common.events import PostCompactEvent
from mote.common.interface import EphemeralContextSource, ObservationSubscriber
from mote.context.turn_context import TeamContextSource


def run(coro):
    return asyncio.run(coro)


# --------------------------------------------------------------------------
# Duck-typed control-plane fakes (no environment import).
# --------------------------------------------------------------------------
class FakePath:
    """Segment-tuple AgentPath with parent() / name() / value equality."""

    def __init__(self, *segments: str):
        self._segs = tuple(segments)

    def parent(self) -> Optional["FakePath"]:
        if not self._segs:
            return None
        return FakePath(*self._segs[:-1])

    def name(self) -> str:
        return self._segs[-1] if self._segs else ""

    def __eq__(self, other) -> bool:
        return isinstance(other, FakePath) and other._segs == self._segs

    def __hash__(self) -> int:
        return hash(self._segs)


class FakeMeta:
    def __init__(self, agent_id, path, *, nickname="", role=""):
        self.agent_id = agent_id
        self.agent_path = path
        self.agent_nickname = nickname
        self.agent_role = role


class FakeStatus:
    def __init__(self, value):
        self.value = value


class FakeRegistry:
    def __init__(self, metas: List[FakeMeta]):
        self._by_id = {m.agent_id: m for m in metas}
        self._by_path = {m.agent_path: m for m in metas}

    def agent_metadata_for_id(self, agent_id):
        return self._by_id.get(agent_id)

    def agent_id_for_path(self, path):
        m = self._by_path.get(path)
        return m.agent_id if m is not None else None

    def live_agents(self):
        # The real registry excludes root; callers only rely on membership.
        return list(self._by_id.values())


class FakeControl:
    def __init__(self, registry, statuses=None):
        self.registry = registry
        self._statuses = statuses or {}

    def get_status(self, agent_id):
        return FakeStatus(self._statuses.get(agent_id, "idle"))


class FakeCtx:
    """A Context stand-in carrying an explicit ``agent_control``."""

    def __init__(self, control):
        self.agent_control = control


def _source(control, session_id="self", **kw):
    return TeamContextSource(get_session_id=lambda: session_id, get_context=lambda: FakeCtx(control), **kw)


def _family():
    """root -> parent -> {self, sib}; self -> child."""
    root = FakeMeta("root", FakePath("root"), nickname="root", role="orchestrator")
    parent = FakeMeta("p1", FakePath("root", "p1"), nickname="Boss", role="lead")
    me = FakeMeta("self", FakePath("root", "p1", "self"), nickname="Me", role="eng")
    sib = FakeMeta("s1", FakePath("root", "p1", "s1"), nickname="Sib", role="qa")
    child = FakeMeta("c1", FakePath("root", "p1", "self", "c1"), nickname="Kid", role="intern")
    reg = FakeRegistry([root, parent, me, sib, child])
    statuses = {"p1": "running", "s1": "idle", "c1": "running"}
    return FakeControl(reg, statuses)


# --------------------------------------------------------------------------
# Protocol / metadata
# --------------------------------------------------------------------------
class TestProtocol:
    def test_is_ephemeral_context_source(self):
        assert isinstance(_source(_family()), EphemeralContextSource)

    def test_is_also_event_subscriber(self):
        # Dual-role: resets the frontier on PostCompactEvent AND renders the delta.
        assert isinstance(_source(_family()), ObservationSubscriber)

    def test_save_to_context_true(self):
        # Persisted + incremental — the roster delta rides history.
        assert _source(_family()).save_to_context is True

    def test_priority_and_name(self):
        s = _source(_family())
        assert s.name == "team" and s.priority == 12


# --------------------------------------------------------------------------
# Self-suppression
# --------------------------------------------------------------------------
class TestSilent:
    def test_no_plane_bound_returns_none(self):
        # No explicit ctx.agent_control and no ambient plane → nothing to report.
        src = TeamContextSource(get_session_id=lambda: "self", get_context=lambda: FakeCtx(None))
        assert run(src.render()) is None

    def test_no_session_id_returns_none(self):
        src = TeamContextSource(get_session_id=lambda: None, get_context=lambda: FakeCtx(_family()))
        assert run(src.render()) is None

    def test_unknown_self_returns_none(self):
        # session id not in the registry → no own metadata → empty roster.
        assert run(_source(_family(), session_id="ghost").render()) is None

    def test_lonely_agent_returns_none(self):
        # A single root-child with no parent/siblings/children.
        only = FakeMeta("solo", FakePath("solo"))
        ctrl = FakeControl(FakeRegistry([only]))
        assert run(_source(ctrl, session_id="solo").render()) is None

    def test_collect_failure_is_swallowed(self):
        class Boom:
            @property
            def registry(self):
                raise RuntimeError("plane blew up")

        assert run(_source(Boom(), session_id="self").render()) is None


# --------------------------------------------------------------------------
# First turn — full roster
# --------------------------------------------------------------------------
class TestFirstTurn:
    def test_emits_parent_siblings_children(self):
        out = run(_source(_family()).render())
        assert out is not None
        assert out.startswith("# Team")
        assert "Parent:" in out and "Siblings:" in out and "Children:" in out
        assert "Boss" in out and "Sib" in out and "Kid" in out

    def test_renders_session_ids(self):
        out = run(_source(_family()).render())
        assert "session=p1" in out and "session=s1" in out and "session=c1" in out

    def test_renders_role_and_status(self):
        out = run(_source(_family()).render())
        assert "role=lead" in out and "status=running" in out

    def test_excludes_self_and_root(self):
        out = run(_source(_family()).render())
        assert "session=self" not in out
        # root is the parent's parent — not in this agent's 1-gen neighbourhood.
        assert "session=root" not in out

    def test_marks_frontier(self):
        src = _source(_family())
        run(src.render())
        assert src._sent_ids == {"p1", "s1", "c1"}


# --------------------------------------------------------------------------
# Incremental behaviour
# --------------------------------------------------------------------------
class TestIncremental:
    def test_second_turn_no_change_silent(self):
        src = _source(_family())
        run(src.render())
        assert run(src.render()) is None

    def test_new_sibling_emitted_incrementally(self):
        ctrl = _family()
        src = _source(ctrl)
        run(src.render())  # sends p1, s1, c1
        new_sib = FakeMeta("s2", FakePath("root", "p1", "s2"), nickname="NewSib", role="qa")
        ctrl.registry._by_id["s2"] = new_sib
        ctrl.registry._by_path[new_sib.agent_path] = new_sib
        out = run(src.render())
        assert out is not None
        assert "NewSib" in out and "session=s2" in out
        assert "session=s1" not in out  # only the delta
        assert src._sent_ids == {"p1", "s1", "c1", "s2"}

    def test_new_child_emitted_incrementally(self):
        ctrl = _family()
        src = _source(ctrl)
        run(src.render())
        new_child = FakeMeta("c2", FakePath("root", "p1", "self", "c2"), nickname="Kid2", role="intern")
        ctrl.registry._by_id["c2"] = new_child
        ctrl.registry._by_path[new_child.agent_path] = new_child
        out = run(src.render())
        assert out is not None
        assert "Kid2" in out and "session=c2" in out
        assert "Children:" in out

    def test_removed_teammate_not_reannounced(self):
        ctrl = _family()
        src = _source(ctrl)
        run(src.render())
        # A sibling leaves the live set; removals are never re-announced.
        del ctrl.registry._by_id["s1"]
        assert run(src.render()) is None


# --------------------------------------------------------------------------
# PostCompact reset
# --------------------------------------------------------------------------
class TestPostCompactReset:
    def test_reset_resends_full_roster(self):
        src = _source(_family())
        run(src.render())
        assert run(src.render()) is None  # steady

        run(src.handle(PostCompactEvent(summary="x")))
        assert src._sent_ids == set()

        out = run(src.render())
        assert out is not None
        assert "session=p1" in out and "session=s1" in out and "session=c1" in out

    def test_handle_ignores_unrelated_events(self):
        src = _source(_family())
        run(src.render())
        run(src.handle(object()))
        assert src._sent_ids == {"p1", "s1", "c1"}


# --------------------------------------------------------------------------
# Ambient-plane discovery (no explicit ctx)
# --------------------------------------------------------------------------
class TestAmbientPlane:
    def test_resolves_ambient_control(self):
        # No get_context provider → resolve_control falls back to the ambient
        # plane bound by the scheduler around a turn.
        src = TeamContextSource(get_session_id=lambda: "self")
        with set_control(_family()):
            out = run(src.render())
        assert out is not None
        assert "session=p1" in out
