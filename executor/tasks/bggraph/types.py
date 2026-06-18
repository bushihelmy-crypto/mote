"""Core data types for :mod:`metagpt.executor.bggraph`.

This module is deliberately dependency-light (``common`` + stdlib only) so that
it can be imported by the pool layer for the ``_LLM_ROUTE_SENTINEL`` marker
without pulling in the engine / graph builder.

The execution model is aligned with **langgraph transitions** (forward frontier
super-steps), *not* a static topological DAG:

* edges are *activations* — a single edge fires its target immediately, a
  waiting-edge (multi-source) is an AND-join,
* cycles are allowed and bounded by ``recursion_limit`` (total node
  activations),
* nodes always return a :class:`Stage` (there is **no** ``Command``).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Coroutine, Optional, Sequence

from pydantic import BaseModel, ConfigDict

# Graph execution exceptions are unified under the global exception package;
# re-exported here so existing ``from .types import GraphRouterError`` (etc.)
# call sites within the bggraph package keep working.
from metagpt.common.exception import (  # noqa: F401
    GraphBatchFailureError,
    GraphNodeRetryExhaustedError,
    GraphNodeTimeoutError,
    GraphParamTypeError,
    GraphRecursionError,
    GraphRouterError,
)

# ---------------------------------------------------------------------------
# Sentinels / well-known node names
# ---------------------------------------------------------------------------

START = "__start__"
END = "__end__"

@dataclass
class LlmPauseResult:
    """Returned by the driver coroutine when the graph pauses on an LLM edge.

    Carries the pause state needed for resume so the pool can snapshot it
    and ``resubmit`` can pick up execution from where it left off.
    """

    state: Any  # GraphState with intermediate results
    completed: set  # nodes that finished before pause
    edge: Any  # _LlmEdge that triggered the pause
    run_state: Any = None  # GraphRunState — authoritative per-node records


# Keep old name as a type reference for isinstance checks.
_LLM_ROUTE_SENTINEL = LlmPauseResult


# ---------------------------------------------------------------------------
# Node status — canonical definition lives in common/schema/node_status.py
# ---------------------------------------------------------------------------

from metagpt.common.schema.node_status import BgStatus  # noqa: E402


# ---------------------------------------------------------------------------
# Authoritative run state — per-node execution records
# ---------------------------------------------------------------------------
#
# ``GraphState`` holds node *data* (results, keyed by node name). It cannot tell
# a legitimately-None result apart from a node that never ran or a node whose
# partial output was written before it failed. Resume therefore must not infer
# completion from ``getattr(state, name) is not None``.
#
# ``GraphRunState`` is the separate, authoritative *execution record*: per-node
# status / attempts / failure reason / timing. The driver writes it as the graph
# runs; the pool snapshots it onto ``TaskMeta``; resume reads it (instead of
# inferring) to decide what is already done and what to re-run. Because the same
# object is reused across resumes, ``attempts`` accumulates so a transient-failing
# node cannot thrash forever.

_TERMINAL_DONE = (BgStatus.SUCCESS, BgStatus.SKIPPED)


@dataclass
class NodeRecord:
    """Authoritative execution record for a single node (not its data)."""

    name: str
    status: BgStatus = BgStatus.PENDING
    attempts: int = 0  # accumulates across resumes (retry budget)
    last_error: Optional[str] = None  # full, untruncated failure text
    started_at: Optional[float] = None
    ended_at: Optional[float] = None
    last_route_key: Optional[str] = None  # last LLM/conditional route taken


@dataclass
class GraphRunState:
    """Per-node execution records for one graph task, durable across resumes."""

    records: dict[str, NodeRecord] = field(default_factory=dict)

    @classmethod
    def for_graph(cls, graph: Any) -> "GraphRunState":
        """Build an empty run state with a PENDING record per declared node."""
        return cls(records={name: NodeRecord(name=name) for name in graph._nodes})

    @classmethod
    def infer_from_state(cls, graph: Any, state: Any) -> "GraphRunState":
        """Best-effort run state for a task that has no recorded one yet.

        Falls back to the legacy inference (a node with a non-None value on the
        state is treated as SUCCESS). Used only to bridge tasks whose snapshot
        predates run-state recording; live runs always carry a real record.
        """
        rs = cls.for_graph(graph)
        for name in graph._nodes:
            if getattr(state, name, None) is not None:
                rec = rs.records[name]
                rec.status = BgStatus.SUCCESS
        return rs

    def get(self, name: str) -> NodeRecord:
        rec = self.records.get(name)
        if rec is None:
            rec = NodeRecord(name=name)
            self.records[name] = rec
        return rec

    def completed_names(self) -> set:
        """Nodes that are authoritatively done (succeeded or skipped)."""
        return {n for n, r in self.records.items() if r.status in _TERMINAL_DONE}

    def running_names(self) -> list:
        return [n for n, r in self.records.items() if r.status == BgStatus.RUNNING]

    def mark_running(self, name: str) -> None:
        rec = self.get(name)
        rec.status = BgStatus.RUNNING
        rec.attempts += 1  # accumulates across resumes — retry budget
        rec.started_at = time.time()
        rec.ended_at = None

    def mark_success(self, name: str, *, route_key: Optional[str] = None) -> None:
        rec = self.get(name)
        rec.status = BgStatus.SUCCESS
        rec.ended_at = time.time()
        rec.last_error = None
        if route_key is not None:
            rec.last_route_key = route_key

    def mark_failed(self, name: str, error: Any) -> None:
        rec = self.get(name)
        rec.status = BgStatus.FAILED
        rec.ended_at = time.time()
        rec.last_error = str(error) if error is not None else None

    def mark_cancelled(self, name: str) -> None:
        rec = self.get(name)
        rec.status = BgStatus.CANCELLED
        rec.ended_at = time.time()

    def mark_skipped(self, name: str) -> None:
        rec = self.get(name)
        rec.status = BgStatus.SKIPPED
        rec.ended_at = time.time()

    def reset(self, name: str) -> None:
        """Re-arm a node for a fresh attempt, preserving its attempt count."""
        rec = self.get(name)
        rec.status = BgStatus.PENDING
        rec.started_at = None
        rec.ended_at = None
        rec.last_error = None


# ---------------------------------------------------------------------------
# Stage / GraphState
# ---------------------------------------------------------------------------


@dataclass
class Stage:
    """Describes how a single node executes (submit → optional poll)."""

    submit: Coroutine
    """Submit action. Awaited immediately; its value is fed to ``poll`` (if any)."""

    poll: Optional[Callable[[Any], Coroutine]] = None
    """Poll factory. ``None`` = synchronous node (submit result is final);
    otherwise it receives the submit result and returns a polling coroutine."""

    name: Optional[str] = None
    """Optional stage name (logging / error notifications)."""

    timeout: Optional[float] = None
    """Per-stage timeout (seconds) for the poll coroutine. ``None`` = no bound."""


class GraphState(BaseModel):
    """Base class for graph state. Subclass and declare input fields.

    Node results are stored back onto the state by node name via ``setattr``
    (``state.<node> = result``), so ``extra="allow"`` is required to let those
    dynamic, non-declared attributes through. Declared fields hold the initial
    inputs; node outputs land in pydantic ``__pydantic_extra__``.
    """

    model_config = ConfigDict(extra="allow")


# ---------------------------------------------------------------------------
# Node / edge definitions
# ---------------------------------------------------------------------------


@dataclass
class _NodeDef:
    name: str
    fn: Callable[[GraphState], Awaitable[Stage]]
    description: str = ""
    params: dict[str, dict] = field(default_factory=dict)
    status: BgStatus = BgStatus.PENDING


@dataclass
class _Edge:
    """A single static edge — fires ``to_node`` as soon as ``from_node`` finishes."""

    from_node: str
    to_node: str


@dataclass
class _WaitingEdge:
    """A multi-source AND-join (langgraph waiting-edge).

    ``to_node`` activates only once *all* ``sources`` have completed.
    """

    sources: tuple[str, ...]
    to_node: str


@dataclass
class _ConditionalEdge:
    """Dynamic routing: ``router(state)`` returns a key → ``mapping[key]``.

    The router runs on the post-completion state, so it can read the node's
    result and may point back upstream (forming a cycle).
    """

    from_node: str
    router: Callable[[GraphState], str]
    mapping: dict[str, str]


@dataclass
class _LlmEdge:
    """LLM-in-the-loop routing edge: pause the graph and push options to the LLM."""

    from_node: str
    prompt: str
    mapping: dict[str, str]  # route_key → target_node


def _as_list(val: Any) -> list:
    """Normalize ``None`` / ``str`` / sequence into a list."""
    if val is None:
        return []
    if isinstance(val, str):
        return [val]
    if isinstance(val, Sequence):
        return list(val)
    return [val]
