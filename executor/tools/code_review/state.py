"""State schema for the code-review pipeline graph.

The topology is **static (N-independent)**: the diff is loaded *inside* the
graph, files accumulate in ``remaining``, and a ring (conditional self-edge)
plus a batched ``asyncio.gather`` node walks through them ``batch_size`` at a
time. ``findings`` is the only reducer channel — each batch's results are
appended via ``operator.add`` so concurrent/sequential batches merge cleanly.
"""
from __future__ import annotations

import operator
from typing import Annotated, List, Optional

from mote.executor.tasks.bggraph import GraphState


class ReviewState(GraphState):
    """Input + intermediate state for the code-review pipeline."""

    # --- User inputs ---
    repo_dir: str = ""
    from_ref: Optional[str] = None
    to_ref: Optional[str] = None
    commit: Optional[str] = None
    batch_size: int = 8
    fmt: str = "text"
    parent_session_id: str = ""

    # --- Intermediate (last-value channels) ---
    raw_diff: str = ""  # load_diff output
    # Files still awaiting review. parse_filter seeds it; review_batch consumes
    # ``batch_size`` per lap and writes back the tail (last-value).
    remaining: List = []
    # Plan-gate output: a one-paragraph review strategy (surfaced in the report
    # header). The plan also reorders ``remaining`` for priority.
    strategy: str = ""

    # --- Reducer channel: findings accumulate across batches ---
    findings: Annotated[List, operator.add] = []
    # review_filter output: the self-critique-kept subset of ``findings``
    # (last-value). ``aggregate`` formats this when present, else ``findings``.
    kept_findings: Optional[List] = None

    # --- Output ---
    report: str = ""
