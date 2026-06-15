"""Core data types for :mod:`metagpt.tasks.bggraph`.

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

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Awaitable, Callable, Coroutine, Optional, Sequence

from pydantic import BaseModel, ConfigDict

# ---------------------------------------------------------------------------
# Sentinels / well-known node names
# ---------------------------------------------------------------------------

START = "__start__"
END = "__end__"

# Returned by the driver coroutine when the graph pauses on an LLM-routing edge.
# The pool inspects ``result is _LLM_ROUTE_SENTINEL`` to treat the task as
# *waiting for route* rather than terminal.
_LLM_ROUTE_SENTINEL = object()


# ---------------------------------------------------------------------------
# Node status
# ---------------------------------------------------------------------------


class NodeStatus(str, Enum):
    """Per-node lifecycle status.

    Shares the first five string values with ``BgStatus`` for str-compatibility
    while adding DAG-specific states.
    """

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    SKIPPED = "skipped"
    WAITING_FOR_ROUTE = "waiting_for_route"


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
    auto_retries: int = 0
    retry_wait: float = 5.0
    status: NodeStatus = NodeStatus.PENDING


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


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class RouterError(Exception):
    """Raised when a conditional-edge router fails or returns an unknown key."""


class GraphRecursionError(Exception):
    """Raised when total node activations exceed ``recursion_limit``."""


class BatchFailureError(Exception):
    """Raised at the terminal step when one or more nodes failed."""

    def __init__(self, failures: list[tuple[str, BaseException]]):
        self.failures = failures
        names = ", ".join(n for n, _ in failures)
        super().__init__(f"Nodes failed: {names}")


def _as_list(val: Any) -> list:
    """Normalize ``None`` / ``str`` / sequence into a list."""
    if val is None:
        return []
    if isinstance(val, str):
        return [val]
    if isinstance(val, Sequence):
        return list(val)
    return [val]
