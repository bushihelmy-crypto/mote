"""Core data types for :mod:`mote.runtime.tools.bggraph`.

This module is deliberately dependency-light (``common`` + stdlib only) so that
it can be imported by the pool layer for the :class:`GraphPause` marker without
pulling in the engine / graph builder.

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
from enum import Enum
from typing import Any, Awaitable, Callable, Coroutine, Optional, Sequence
from uuid import uuid4

from pydantic import BaseModel, ConfigDict

# Graph execution exceptions are unified under the global exception package;
# re-exported here so existing ``from .types import GraphRouterError`` (etc.)
# call sites within the bggraph package keep working.
from mote.runtime.errors import (  # noqa: F401
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


class PauseReason(str, Enum):
    """Why the driver coroutine paused instead of reaching a terminal.

    Both reasons produce the SAME resumable snapshot (state / completed /
    run_state) and travel the SAME machinery (pool snapshot → ``resume_tasks``);
    they differ only in what decision the model must make:

    * ``LLM_ROUTE`` — the frontier hit an LLM edge: the model picks a route
      (``resume_tasks(from_node=...)``).
    * ``STALL`` — the frontier drained with a blocked AND-join (a waiting-edge
      whose target can never fire because a source is unreachable): a deadlock
      the model must break (re-run / skip the missing upstream, or accept the
      partial result). Without this the run would silently report SUCCESS with
      the join's downstream never executed.
    """

    LLM_ROUTE = "llm_route"
    STALL = "stall"


@dataclass
class GraphPause:
    """Returned by the driver coroutine when the graph pauses (not terminal).

    One reason-tagged snapshot for every pause: the pool maps :attr:`reason` to
    a resumable :class:`BgStatus` and saves the snapshot; ``resume_tasks`` picks
    execution up from where it left off. Reason-specific context rides alongside
    (``edge`` for an LLM route, ``stalled_nodes`` for a deadlock) so one type
    carries both without a parallel class hierarchy.
    """

    reason: PauseReason
    state: Any  # GraphState with intermediate results
    completed: set  # nodes that finished before pause
    run_state: Any = None  # GraphRunState — authoritative per-node records
    edge: Any = None  # _LlmEdge that triggered an LLM_ROUTE pause
    stalled_nodes: tuple[str, ...] = ()  # blocked AND-join targets for a STALL


# ---------------------------------------------------------------------------
# Node status — canonical definition lives in common/schema/node_status.py
# ---------------------------------------------------------------------------

from mote.orchestration.tasks.status import BgStatus  # noqa: E402

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
    # State fields this node wrote on its last successful run (the keys of the
    # update dict merged into the state). The field/channel model has no static
    # per-node output declaration, so this runtime record IS the truthful answer
    # to "what does this node produce" — read by GetNodeState to show concrete
    # outputs / resolve downstream consumers.
    writes: list[str] = field(default_factory=list)
    # Auto-retries consumed / allowed on the *last* failure (engine policy), used
    # by the notification renderer. Lives here rather than monkey-patched onto the
    # failure exception so the run record is the single source of truth.
    retries_attempted: int = 0
    retries_limit: int = 0


@dataclass
class GraphRunState:
    """Per-node execution records for one graph task, durable across resumes."""

    records: dict[str, NodeRecord] = field(default_factory=dict)
    run_id: str = field(default_factory=lambda: uuid4().hex)

    @classmethod
    def for_graph(cls, graph: Any) -> "GraphRunState":
        """Build an empty run state with a PENDING record per declared node."""
        return cls(records={name: NodeRecord(name=name) for name in graph._nodes})

    @classmethod
    def infer_from_state(cls, graph: Any, state: Any) -> "GraphRunState":
        """Empty (all-PENDING) run state for a task that has no recorded one.

        With the field/channel state model, node results are merged into state
        *fields* (not stored under the node's own name), so completion can no
        longer be inferred from ``getattr(state, node) is not None``. Live runs
        always carry an authoritative ``GraphRunState``; this fallback only
        bridges callers without one, and yields an empty record set.
        """
        return cls.for_graph(graph)

    @classmethod
    def ensure(cls, graph: Any, state: Any, run_state: Optional["GraphRunState"]) -> "GraphRunState":
        """Return *run_state* when present, else infer one (fallback).

        Live runs always thread their authoritative ``run_state`` in; this only
        bridges callers (snapshots / tests) that have none, recovering a
        best-effort state via :meth:`infer_from_state`.
        """
        if run_state is not None:
            return run_state
        return cls.infer_from_state(graph, state)

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

    def mark_success(
        self,
        name: str,
        *,
        route_key: Optional[str] = None,
        writes: Optional[list[str]] = None,
    ) -> None:
        rec = self.get(name)
        rec.status = BgStatus.SUCCESS
        rec.ended_at = time.time()
        rec.last_error = None
        if route_key is not None:
            rec.last_route_key = route_key
        if writes is not None:
            rec.writes = list(writes)

    def mark_failed(
        self,
        name: str,
        error: Any,
        *,
        retries_attempted: int = 0,
        retries_limit: int = 0,
    ) -> None:
        rec = self.get(name)
        rec.status = BgStatus.FAILED
        rec.ended_at = time.time()
        rec.last_error = str(error) if error is not None else None
        rec.retries_attempted = retries_attempted
        rec.retries_limit = retries_limit

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
    """Base class for graph state. Subclass and declare input + output fields.

    State sync is **field/channel-based** (langgraph model): a node returns a
    ``dict`` of field updates (``{field: value}``) which the engine merges into
    the declared state fields. A field annotated ``Annotated[T, reducer]`` is a
    reducer channel — multiple writers are combined via the reducer (e.g.
    ``Annotated[list, operator.add]`` appends); a plain field is last-value
    (most recent write wins).

    ``extra="allow"`` is kept so a node may also write undeclared keys (they
    land last-value in ``__pydantic_extra__``); declaring the field is preferred
    so reducers and type/introspection apply.
    """

    model_config = ConfigDict(extra="allow")


# ---------------------------------------------------------------------------
# Node / edge definitions
# ---------------------------------------------------------------------------


@dataclass
class _NodeDef:
    """Static node definition — intentionally stateless.

    A compiled graph is shared across concurrent / resumed runs, so per-run
    execution state (status / attempts / timing) lives on the per-run
    :class:`GraphRunState`, never here. (Aligned with langgraph's stateless
    ``PregelNode``.)
    """

    name: str
    fn: Callable[[GraphState], Awaitable[Stage]]
    description: str = ""
    params: dict[str, dict] = field(default_factory=dict)


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
