"""Tests for the ``Output`` field marker and success-result narrowing.

A graph's success result is its *declared output* — only the state fields
marked ``Annotated[T, Output]`` — so inputs and intermediate scratch never leak
into what is returned / pushed to the model. A graph that declares no output
falls back to the whole final state (back-compat).
"""
from __future__ import annotations

from typing import Annotated

import pytest

from mote.executor.tasks.bggraph import END, START, BgGraph, GraphState, Output, Stage
from mote.executor.tasks.bggraph.channels import derive_output_fields


def _sync_node(fn, *, field=None):
    async def node(state):
        async def submit():
            result = fn(state)
            return {field: result} if field is not None else result

        return Stage(submit=submit())

    return node


# ---------------------------------------------------------------------------
# derive_output_fields — unit
# ---------------------------------------------------------------------------


class _OutState(GraphState):
    src: str = ""  # input, unmarked
    scratch: str = ""  # intermediate, unmarked
    report: Annotated[str, Output] = ""  # marked (class sentinel)
    extra: Annotated[dict, Output()] = {}  # marked (instance also accepted)


class _NoOutState(GraphState):
    a: int = 0
    b: int = 0


def test_derive_output_fields_collects_marked_only():
    assert derive_output_fields(_OutState) == {"report", "extra"}


def test_derive_output_fields_empty_when_none_marked():
    assert derive_output_fields(_NoOutState) == set()


# ---------------------------------------------------------------------------
# Engine — success returns only the declared output
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_success_result_is_only_declared_output():
    g = BgGraph("narrow", state_schema=_OutState)
    # write both an output field and an intermediate one
    g.add_node("work", _sync_node(lambda s: {"report": "R", "scratch": "S"}))
    g.add_edge(START, "work")
    g.add_edge("work", END)

    res = await g.compile()(src="IN")
    result = await res.poll_factory()

    # Only Output-marked fields come back — input (src) and intermediate
    # (scratch) are excluded, even though they exist on the final state.
    assert set(result) == {"report", "extra"}
    assert result["report"] == "R"
    assert "src" not in result
    assert "scratch" not in result


@pytest.mark.asyncio
async def test_no_output_declaration_falls_back_to_full_state():
    g = BgGraph("full", state_schema=_NoOutState)
    g.add_node("work", _sync_node(lambda s: {"a": 1, "b": 2}))
    g.add_edge(START, "work")
    g.add_edge("work", END)

    res = await g.compile()(a=0, b=0)
    result = await res.poll_factory()

    # No field marked → whole final state returned (langgraph .invoke() parity).
    assert result == {"a": 1, "b": 2}
