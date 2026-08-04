#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Unit tests for the ComponentGraph build engine (roles/component_graph.py).

Covers: lazy build + caching, eager (``dep``) vs deferred (``defer``) sibling
edges, build-time cycle detection (the whole point — a construction cycle raises
a clear error instead of a stack overflow), opt-in ``available`` gating with
``None`` never cached, ``peek`` never building, and unknown-name / duplicate-spec
errors.
"""

from __future__ import annotations

import pytest

from mote.runtime.agent.component_graph import (
    ComponentCycleError,
    ComponentGraph,
    ComponentGraphError,
    ComponentKey,
    ComponentSpec,
    UnknownComponentError,
)


def _key(name: str) -> ComponentKey:
    return ComponentKey(name)


class _Role:
    """A stand-in role: the builders only read what a spec chooses to."""

    def __init__(self):
        self.calls: list[str] = []


class _State:
    def __init__(self):
        self.hook_layer = False


def _graph(specs, role=None, state=None) -> ComponentGraph:
    return ComponentGraph(role or _Role(), state or _State(), specs)


# --------------------------------------------------------------------------- #
# Lazy build + caching
# --------------------------------------------------------------------------- #
def test_get_builds_lazily_and_caches():
    role = _Role()

    def build_x(ctx):
        ctx.role.calls.append("x")
        return object()

    x = _key("x")
    g = _graph([ComponentSpec(x, build_x)], role=role)
    assert role.calls == []  # not built until asked
    first = g.get(x)
    assert role.calls == ["x"]
    assert g.get(x) is first  # cached — builder runs once
    assert role.calls == ["x"]


def test_component_resolution_does_not_define_a_lifecycle_side_effect():
    """Factories construct objects; explicit lifecycle methods perform work."""
    calls: list[str] = []

    class Managed:
        def prepare(self):
            calls.append("prepare")

    managed = _key("managed")
    g = _graph([ComponentSpec(managed, lambda ctx: Managed())])
    component = g.get(managed)

    assert calls == []
    component.prepare()
    assert calls == ["prepare"]


def test_is_built_reflects_state():
    x = _key("x")
    g = _graph([ComponentSpec(x, lambda ctx: object())])
    assert g.is_built(x) is False
    g.get(x)
    assert g.is_built(x) is True


# --------------------------------------------------------------------------- #
# Eager (dep) vs deferred (defer) edges
# --------------------------------------------------------------------------- #
def test_dep_resolves_sibling_eagerly():
    leaf, parent = _key("leaf"), _key("parent")
    g = _graph(
        [
            ComponentSpec(leaf, lambda ctx: "LEAF"),
            ComponentSpec(parent, lambda ctx: f"parent({ctx.dep(leaf)})"),
        ]
    )
    assert g.get(parent) == "parent(LEAF)"
    assert g.is_built(leaf) is True  # eager edge forced the leaf


def test_defer_does_not_build_until_called():
    leaf, parent = _key("leaf"), _key("parent")
    g = _graph(
        [
            ComponentSpec(leaf, lambda ctx: "LEAF"),
            ComponentSpec(parent, lambda ctx: ctx.defer(leaf)),
        ]
    )
    thunk = g.get(parent)
    assert g.is_built(leaf) is False  # deferred — not built yet
    assert thunk() == "LEAF"  # resolves on call
    assert g.is_built(leaf) is True


# --------------------------------------------------------------------------- #
# BuildContext isolation — dep/defer are the ONLY handle on the resolver
# --------------------------------------------------------------------------- #
# The engine's guarantees (cycle detection, availability gating) hold only for
# sibling access the resolver can see. If a builder could reach the graph off-book
# — pull ``get``/``seed`` directly, or dirty-write ``_slots`` mid-build — it could
# forge an edge the cycle check never sees or skip an ``available`` gate. So the
# context closes over the graph inside ``dep``/``defer`` and exposes no attribute
# leading back to it: the isolation is structural, not a documented convention.
def test_context_exposes_no_handle_on_the_graph():
    captured = {}

    def build(ctx):
        # No ``_graph`` (or any instance data) a builder could use to reach
        # ``get``/``seed``/``_slots``: the resolver is only closed over inside the
        # ``dep``/``defer`` primitives, not stored on the context.
        captured["has_graph"] = hasattr(ctx, "_graph")
        # ``__slots__`` is the exhaustive set of instance attributes; the two
        # ``_make_*`` names on the *class* are static edge-primitive factories
        # that need the graph passed explicitly, so they are not a usable handle.
        captured["slots"] = set(type(ctx).__slots__)
        return "ok"

    x = _key("x")
    g = _graph([ComponentSpec(x, build)])
    assert g.get(x) == "ok"
    assert captured["has_graph"] is False
    assert captured["slots"] == {"role", "state", "dep", "defer"}


def test_context_forbids_stashing_arbitrary_attributes():
    # ``__slots__`` means a builder can't smuggle state onto the context either
    # (which would otherwise be a channel for a builder to mutate a sibling's
    # inputs behind the resolver's back).
    def build(ctx):
        ctx.sneaky = 1  # no slot for it

    x = _key("x")
    g = _graph([ComponentSpec(x, build)])
    with pytest.raises(AttributeError):
        g.get(x)


def test_dep_defer_still_resolve_after_closure_refactor():
    # The closure-over-graph rewrite must not change the edge semantics: dep is
    # eager (forces the sibling now), defer is a lazy thunk (builds on call).
    leaf, eager, lazy = _key("leaf"), _key("eager"), _key("lazy")
    g = _graph(
        [
            ComponentSpec(leaf, lambda ctx: "LEAF"),
            ComponentSpec(eager, lambda ctx: ctx.dep(leaf)),
            ComponentSpec(lazy, lambda ctx: ctx.defer(leaf)),
        ]
    )
    assert g.get(eager) == "LEAF"
    thunk = g.get(lazy)
    assert callable(thunk) and thunk() == "LEAF"


# --------------------------------------------------------------------------- #
# Cycle detection — the point of the engine
# --------------------------------------------------------------------------- #
def test_direct_cycle_raises_not_stack_overflow():
    a = _key("a")
    g = _graph([ComponentSpec(a, lambda ctx: ctx.dep(a))])
    with pytest.raises(ComponentCycleError) as exc:
        g.get(a)
    assert "a -> a" in str(exc.value)


def test_indirect_cycle_raises_with_path():
    a, b, c = _key("a"), _key("b"), _key("c")
    g = _graph(
        [
            ComponentSpec(a, lambda ctx: ctx.dep(b)),
            ComponentSpec(b, lambda ctx: ctx.dep(c)),
            ComponentSpec(c, lambda ctx: ctx.dep(a)),
        ]
    )
    with pytest.raises(ComponentCycleError) as exc:
        g.get(a)
    assert "a -> b -> c -> a" in str(exc.value)


def test_deferred_edge_breaks_a_cycle():
    # a -> b (eager), b -> a (deferred): no construction cycle — b captures a
    # thunk instead of building a, so both resolve.
    a_key, b_key = _key("a"), _key("b")
    g = _graph(
        [
            ComponentSpec(a_key, lambda ctx: {"me": "a", "b": ctx.dep(b_key)}),
            ComponentSpec(b_key, lambda ctx: {"me": "b", "get_a": ctx.defer(a_key)}),
        ]
    )
    a = g.get(a_key)
    assert a["b"]["me"] == "b"
    assert a["b"]["get_a"]()["me"] == "a"  # thunk resolves the cached a


def test_resolution_stack_unwinds_after_cycle():
    # After a cycle raises, the stack must be clean so unrelated resolves still work.
    a, ok = _key("a"), _key("ok")
    g = _graph(
        [
            ComponentSpec(a, lambda ctx: ctx.dep(a)),
            ComponentSpec(ok, lambda ctx: "OK"),
        ]
    )
    with pytest.raises(ComponentCycleError):
        g.get(a)
    assert g.get(ok) == "OK"


# --------------------------------------------------------------------------- #
# Opt-in availability + None-not-cached
# --------------------------------------------------------------------------- #
def test_unavailable_layer_returns_none_and_is_not_cached():
    state = _State()
    calls = {"n": 0}

    def build_hook(ctx):
        calls["n"] += 1
        return "HOOK"

    hook = _key("hook")
    g = _graph(
        [ComponentSpec(hook, build_hook, available=lambda role, st: st.hook_layer)],
        state=state,
    )
    assert g.get(hook) is None  # gated off
    assert calls["n"] == 0  # builder never ran
    assert g.is_built(hook) is False

    state.hook_layer = True  # engage the layer later
    assert g.get(hook) == "HOOK"  # now it builds
    assert calls["n"] == 1
    assert g.get(hook) == "HOOK"  # and caches
    assert calls["n"] == 1


def test_builder_returning_none_is_not_cached():
    calls = {"n": 0}

    def build(ctx):
        calls["n"] += 1
        return None  # precondition unmet (e.g. file-watch, no consumer)

    fw = _key("fw")
    g = _graph([ComponentSpec(fw, build)])
    assert g.get(fw) is None
    assert g.get(fw) is None
    assert calls["n"] == 2  # re-evaluated each time (not cached)


# --------------------------------------------------------------------------- #
# peek — never builds
# --------------------------------------------------------------------------- #
def test_peek_never_builds():
    calls = {"n": 0}

    def build(ctx):
        calls["n"] += 1
        return "X"

    x = _key("x")
    g = _graph([ComponentSpec(x, build)])
    assert g.peek(x) is None
    assert calls["n"] == 0
    g.get(x)
    assert g.peek(x) == "X"
    assert calls["n"] == 1


# --------------------------------------------------------------------------- #
# Registry errors
# --------------------------------------------------------------------------- #
def test_unknown_component_raises():
    x, nope = _key("x"), _key("nope")
    g = _graph([ComponentSpec(x, lambda ctx: 1)])
    with pytest.raises(UnknownComponentError):
        g.get(nope)
    with pytest.raises(UnknownComponentError):
        g.peek(nope)


def test_same_diagnostic_name_cannot_forge_registered_key():
    registered = _key("x")
    forged = _key("x")
    g = _graph([ComponentSpec(registered, lambda ctx: 1)])

    with pytest.raises(UnknownComponentError):
        g.get(forged)


def test_duplicate_spec_raises():
    x = _key("x")
    with pytest.raises(ComponentGraphError):
        _graph([ComponentSpec(x, lambda ctx: 1), ComponentSpec(x, lambda ctx: 2)])
