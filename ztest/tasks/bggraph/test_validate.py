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

from metagpt.tasks.bggraph import END, START, BgGraph

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
