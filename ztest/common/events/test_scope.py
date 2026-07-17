"""Tests for the activity-scope contextvar spine (``common/events/scope.py``)."""

from __future__ import annotations

import asyncio

import pytest

from mote.common.events.scope import ScopeRef, current_scope, push_scope


def test_default_is_empty():
    assert current_scope() == ()


def test_nested_push_accumulates():
    with push_scope(ScopeRef("graph", "g1", "run_graph")):
        assert current_scope() == (ScopeRef("graph", "g1", "run_graph"),)
        with push_scope(ScopeRef("node", "n1", "translate")):
            path = current_scope()
            assert len(path) == 2
            assert path[0].kind == "graph"
            assert path[1] == ScopeRef("node", "n1", "translate")
        # inner popped
        assert current_scope() == (ScopeRef("graph", "g1", "run_graph"),)
    assert current_scope() == ()


def test_finally_resets_on_exception():
    with pytest.raises(RuntimeError):
        with push_scope(ScopeRef("graph", "g1", "run_graph")):
            raise RuntimeError("boom")
    assert current_scope() == ()


def test_push_returns_extended_path():
    with push_scope(ScopeRef("agent", "a1", "worker")) as path:
        assert path == current_scope()
        assert path == (ScopeRef("agent", "a1", "worker"),)


@pytest.mark.asyncio
async def test_scope_propagates_into_gathered_children():
    seen: list[tuple] = []

    async def child(i: int):
        # A task spawned under an ambient scope inherits it (contextvar copy).
        seen.append((i, current_scope()))

    with push_scope(ScopeRef("graph", "g1", "map")):
        await asyncio.gather(*(child(i) for i in range(3)))

    assert len(seen) == 3
    for _, scope in seen:
        assert scope == (ScopeRef("graph", "g1", "map"),)


@pytest.mark.asyncio
async def test_child_push_does_not_leak_to_sibling():
    outer = ScopeRef("graph", "g", "g")
    results: dict[int, tuple] = {}

    async def child(i: int):
        with push_scope(ScopeRef("node", f"n{i}", f"n{i}")):
            await asyncio.sleep(0)
            results[i] = current_scope()

    with push_scope(outer):
        await asyncio.gather(child(0), child(1))
        # After children complete, the outer scope is intact here.
        assert current_scope() == (outer,)

    assert results[0] == (outer, ScopeRef("node", "n0", "n0"))
    assert results[1] == (outer, ScopeRef("node", "n1", "n1"))
