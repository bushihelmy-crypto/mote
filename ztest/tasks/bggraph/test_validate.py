#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Validation tests for :meth:`BgGraph._validate` / ``_validate_params``.

The langgraph model **allows cycles** — there is intentionally no compile-time
no-cycle check.  ``_validate`` only enforces: every referenced node exists,
exactly one edge from ``START``, at least one edge to ``END``, and well-formed
waiting / conditional / llm edges.  ``_validate_params`` checks ``params['from']``
references against the state schema / known nodes.
"""
from __future__ import annotations

import pytest

from metagpt.executor.tasks.bggraph import END, START, BgGraph, GraphState

from .conftest import S, sync_node


def _node(g, name, fn=lambda s: None):
    g.add_node(name, sync_node(fn))


class TestEdgeReferences:
    def test_unknown_edge_target(self):
        g = BgGraph("g", state_schema=S)
        _node(g, "a")
        g.add_edge(START, "a")
        g.add_edge("a", "ghost")  # ghost not a node
        with pytest.raises(ValueError, match="Unknown node"):
            g.compile()

    def test_unknown_waiting_source(self):
        g = BgGraph("g", state_schema=S)
        _node(g, "a")
        _node(g, "m")
        g.add_edge(START, "a")
        g.add_edge(["a", "ghost"], "m")
        g.add_edge("m", END)
        with pytest.raises(ValueError, match="Unknown source in waiting edge"):
            g.compile()

    def test_unknown_conditional_target(self):
        g = BgGraph("g", state_schema=S)
        _node(g, "a")
        g.add_edge(START, "a")
        g.add_conditional_edges("a", lambda s: "k", {"k": "ghost"})
        with pytest.raises(ValueError, match="Unknown target in conditional edge"):
            g.compile()

    def test_unknown_llm_target(self):
        g = BgGraph("g", state_schema=S)
        _node(g, "a")
        g.add_edge(START, "a")
        g.add_llm_edges("a", "prompt", {"k": "ghost"})
        with pytest.raises(ValueError, match="Unknown target in LLM edge"):
            g.compile()


class TestStartEnd:
    def test_no_start_edge(self):
        g = BgGraph("g", state_schema=S)
        _node(g, "a")
        g.add_edge("a", END)
        with pytest.raises(ValueError, match="exactly one edge from START"):
            g.compile()

    def test_two_start_edges(self):
        g = BgGraph("g", state_schema=S)
        _node(g, "a")
        _node(g, "b")
        g.add_edge(START, "a")
        g.add_edge(START, "b")
        g.add_edge("a", END)
        g.add_edge("b", END)
        with pytest.raises(ValueError, match="exactly one edge from START"):
            g.compile()

    def test_no_end_edge(self):
        g = BgGraph("g", state_schema=S)
        _node(g, "a")
        _node(g, "b")
        g.add_edge(START, "a")
        g.add_edge("a", "b")  # never reaches END
        with pytest.raises(ValueError, match="at least one edge to END"):
            g.compile()

    def test_end_via_conditional_ok(self):
        g = BgGraph("g", state_schema=S)
        _node(g, "a")
        g.add_edge(START, "a")
        g.add_conditional_edges("a", lambda s: "k", {"k": END})
        g.compile()  # no raise

    def test_end_via_llm_ok(self):
        g = BgGraph("g", state_schema=S)
        _node(g, "a")
        g.add_edge(START, "a")
        g.add_llm_edges("a", "prompt", {"stop": END})
        g.compile()  # no raise


class TestCyclesAllowed:
    def test_cycle_compiles(self):
        g = BgGraph("g", state_schema=S)
        _node(g, "a")
        _node(g, "b")
        g.add_edge(START, "a")
        g.add_edge("a", "b")
        g.add_conditional_edges("b", lambda s: "loop", {"loop": "a", "done": END})
        g.compile()  # cycles are intentionally allowed


class TestWaitingEdgeNormalization:
    """Static channels into an AND-join target are *merged*, not rejected.

    A waiting-edge fires its target exactly once, after all sources arrive. If a
    plain single edge or a second waiting-edge also targets it, the only fire-once
    reading is "wait for the union of all sources" — so ``_normalize_waiting_edges``
    folds them into one AND-join. A *dynamic* route (conditional / LLM) into the
    join target can't be folded and is still rejected.
    """

    def _join_for(self, g, target):
        joins = [we for we in g._waiting_edges if we.to_node == target]
        assert len(joins) == 1, f"expected one merged join for {target}, got {joins}"
        return joins[0]

    def test_single_edge_folded_into_join(self):
        # merge ← a (single) AND merge ← [b, c] (join) → merge ← [b, c, a].
        g = BgGraph("g", state_schema=S)
        _node(g, "a")
        _node(g, "b")
        _node(g, "c")
        _node(g, "merge")
        g.add_edge(START, "a")
        g.add_edge("a", "b")
        g.add_edge("a", "c")
        g.add_edge("a", "merge")  # folded into the join
        g.add_edge(["b", "c"], "merge")
        g.add_edge("merge", END)
        g.compile()  # no raise
        join = self._join_for(g, "merge")
        assert set(join.sources) == {"a", "b", "c"}
        # The absorbed single edge a→merge is gone.
        assert not any(
            e.from_node == "a" and e.to_node == "merge" for e in g._edges
        )

    def test_two_waiting_edges_merged(self):
        g = BgGraph("g", state_schema=S)
        _node(g, "a")
        _node(g, "b")
        _node(g, "c")
        _node(g, "merge")
        g.add_edge(START, "a")
        g.add_edge("a", "b")
        g.add_edge("a", "c")
        g.add_edge(["a", "b"], "merge")
        g.add_edge(["b", "c"], "merge")  # union → [a, b, c]
        g.add_edge("merge", END)
        g.compile()  # no raise
        join = self._join_for(g, "merge")
        assert set(join.sources) == {"a", "b", "c"}

    def test_conditional_route_into_join_still_rejected(self):
        # A sometimes-firing route can't join an all-must-arrive merge.
        g = BgGraph("g", state_schema=S)
        _node(g, "a")
        _node(g, "b")
        _node(g, "c")
        _node(g, "merge")
        g.add_edge(START, "a")
        g.add_edge("a", "b")
        g.add_conditional_edges("a", lambda s: "k", {"k": "c"})
        g.add_conditional_edges("c", lambda s: "k", {"k": "merge"})  # dynamic
        g.add_edge(["b", "c"], "merge")
        g.add_edge("merge", END)
        with pytest.raises(ValueError, match="dynamic route target"):
            g.compile()

    def test_clean_waiting_edge_unchanged(self):
        # merge has exactly one trigger channel → kept verbatim.
        g = BgGraph("g", state_schema=S)
        _node(g, "a")
        _node(g, "b")
        _node(g, "c")
        _node(g, "merge")
        g.add_edge(START, "a")
        g.add_edge("a", "b")
        g.add_edge("a", "c")
        g.add_edge(["b", "c"], "merge")
        g.add_edge("merge", END)
        g.compile()  # no raise
        join = self._join_for(g, "merge")
        assert set(join.sources) == {"b", "c"}

    def test_cycle_through_join_to_sources_compiles(self):
        # Looping back to the join's *sources* (not the target) is legal: the
        # join re-collects them each lap. (Mirrors test_engine's re-wait test.)
        g = BgGraph("g", state_schema=S)
        _node(g, "a")
        _node(g, "fast")
        _node(g, "slow")
        _node(g, "merge")
        g.add_edge(START, "a")
        g.add_edge("a", "fast")
        g.add_edge("a", "slow")
        g.add_edge(["fast", "slow"], "merge")
        g.add_conditional_edges("merge", lambda s: "loop", {"loop": "a", "done": END})
        g.compile()  # no raise — loop targets 'a', not the join target 'merge'

    def test_join_to_end_left_untouched(self):
        # END is special — many nodes legitimately finish there; not merged.
        g = BgGraph("g", state_schema=S)
        _node(g, "a")
        _node(g, "b")
        _node(g, "c")
        g.add_edge(START, "a")
        g.add_edge("a", "b")
        g.add_edge("a", "c")
        g.add_edge("a", END)
        g.add_edge(["b", "c"], END)
        g.compile()  # no raise — END is exempt
        # The single edge a→END survives (not folded into the END join).
        assert any(e.from_node == "a" and e.to_node == END for e in g._edges)


class TestParamValidation:
    def test_unknown_input_field(self):
        g = BgGraph("g", state_schema=S)
        g.add_node(
            "a", sync_node(lambda s: 1), params={"p": {"from": "$input.nope", "desc": "d"}}
        )
        g.add_edge(START, "a")
        g.add_edge("a", END)
        with pytest.raises(ValueError, match="unknown input field"):
            g.compile()

    def test_known_input_field_ok(self):
        g = BgGraph("g", state_schema=S)
        g.add_node(
            "a", sync_node(lambda s: 1), params={"p": {"from": "$input.x", "desc": "d"}}
        )
        g.add_edge(START, "a")
        g.add_edge("a", END)
        g.compile()  # no raise

    def test_unknown_node_ref(self):
        g = BgGraph("g", state_schema=S)
        g.add_node("a", sync_node(lambda s: 1), params={"p": {"from": "ghost.out"}})
        g.add_edge(START, "a")
        g.add_edge("a", END)
        with pytest.raises(ValueError, match="unknown node"):
            g.compile()


class TestParamTypeValidation:
    """Compile-time type compatibility checks for $input params."""

    def test_type_mismatch_input_field(self):
        """state.x is int, param declares str → ValueError."""
        g = BgGraph("g", state_schema=S)
        g.add_node(
            "a",
            sync_node(lambda s: 1),
            params={"p": {"from": "$input.x", "desc": "d", "type": str}},
        )
        g.add_edge(START, "a")
        g.add_edge("a", END)
        with pytest.raises(ValueError, match="expected.*str"):
            g.compile()

    def test_type_compatible_passes(self):
        """state.x is int, param declares int → OK."""
        g = BgGraph("g", state_schema=S)
        g.add_node(
            "a",
            sync_node(lambda s: 1),
            params={"p": {"from": "$input.x", "desc": "d", "type": int}},
        )
        g.add_edge(START, "a")
        g.add_edge("a", END)
        g.compile()  # no raise

    def test_no_type_skips_check(self):
        """param without type → no type check, compiles fine even if field is int."""
        g = BgGraph("g", state_schema=S)
        g.add_node(
            "a",
            sync_node(lambda s: 1),
            params={"p": {"from": "$input.x", "desc": "d"}},
        )
        g.add_edge(START, "a")
        g.add_edge("a", END)
        g.compile()  # no raise — type key absent

    def test_no_type_none_skips_check(self):
        """param with type=None → no type check."""
        g = BgGraph("g", state_schema=S)
        g.add_node(
            "a",
            sync_node(lambda s: 1),
            params={"p": {"from": "$input.x", "desc": "d", "type": None}},
        )
        g.add_edge(START, "a")
        g.add_edge("a", END)
        g.compile()  # no raise

    def test_optional_unwrap(self):
        """Optional[int] field is compatible with int param type."""
        from typing import Optional

        class OptState(GraphState):
            x: Optional[int] = None

        g = BgGraph("g", state_schema=OptState)
        g.add_node(
            "a",
            sync_node(lambda s: 1),
            params={"p": {"from": "$input.x", "desc": "d", "type": int}},
        )
        g.add_edge(START, "a")
        g.add_edge("a", END)
        g.compile()  # no raise — Optional[int] is compatible with int

    def test_subclass_compatible(self):
        """bool subclasses int → param type int, field bool → OK."""

        class BoolState(GraphState):
            flag: bool = False

        g = BgGraph("g", state_schema=BoolState)
        g.add_node(
            "a",
            sync_node(lambda s: 1),
            params={"p": {"from": "$input.flag", "desc": "d", "type": int}},
        )
        g.add_edge(START, "a")
        g.add_edge("a", END)
        g.compile()  # no raise — bool is subclass of int

    def test_node_ref_type_skipped(self):
        """Params referencing another node's output skip type check at compile time."""
        g = BgGraph("g", state_schema=S)
        g.add_node(
            "a",
            sync_node(lambda s: "hello"),
        )
        g.add_node(
            "b",
            sync_node(lambda s: 1),
            params={"p": {"from": "a.output", "desc": "d", "type": int}},
        )
        g.add_edge(START, "a")
        g.add_edge("a", "b")
        g.add_edge("b", END)
        g.compile()  # no raise — node refs are runtime-only
