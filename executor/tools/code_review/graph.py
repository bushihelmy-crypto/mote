"""Build the code-review pipeline as a BgGraph.

Topology (static, N-independent — the ring walks the file list)::

    START → load_diff → parse_filter → plan → review_batch ⇄ router → {
        loop: review_batch,     # remaining non-empty
        done: review_filter,    # remaining empty
    }
    review_filter → aggregate → END

- load_diff: fetch the git diff (deterministic)
- parse_filter: parse + filter to reviewable files, attach related-file hints
  (deterministic)
- plan: prioritize the file order + emit a strategy note (agent, best-effort)
- review_batch: review up to batch_size files concurrently (agent leaves),
  self-loops until ``remaining`` is drained
- review_filter: self-critique the findings, dropping low-value ones (agent,
  best-effort)
- aggregate: format the kept findings into the report (deterministic)
"""
from __future__ import annotations

from metagpt.executor.tasks.bggraph import BgGraph, START, END

from .nodes import (
    _route_after_batch,
    aggregate_node,
    load_diff_node,
    parse_filter_node,
    plan_node,
    review_batch_node,
    review_filter_node,
)
from .state import ReviewState


def build_code_review_graph() -> BgGraph:
    """Construct and return the code-review graph (not yet compiled).

    ``recursion_limit`` is sized generously for the ring: each lap is one
    ``review_batch`` activation, so the bound is roughly the number of batches
    (files / batch_size) plus the skeleton nodes. 200 covers very large
    changesets without risking a runaway.
    """
    g = BgGraph("code_review", state_schema=ReviewState, recursion_limit=200)

    g.add_node("load_diff", load_diff_node)
    g.add_node("parse_filter", parse_filter_node)
    g.add_node("plan", plan_node)
    g.add_node("review_batch", review_batch_node)
    g.add_node("review_filter", review_filter_node)
    g.add_node("aggregate", aggregate_node)

    g.add_edge(START, "load_diff")
    g.add_edge("load_diff", "parse_filter")
    g.add_edge("parse_filter", "plan")
    g.add_edge("plan", "review_batch")
    g.add_conditional_edges(
        "review_batch",
        _route_after_batch,
        {"loop": "review_batch", "done": "review_filter"},
    )
    g.add_edge("review_filter", "aggregate")
    g.add_edge("aggregate", END)

    return g
