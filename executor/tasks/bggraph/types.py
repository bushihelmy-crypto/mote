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

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Coroutine, Optional, Sequence

from pydantic import BaseModel, ConfigDict

# Graph execution exceptions are unified under the global exception package;
# re-exported here so existing ``from .types import GraphRouterError`` (etc.)
# call sites within the bggraph package keep working.
from metagpt.common.exception import (  # noqa: F401
    GraphBatchFailureError,
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


# Keep old name as a type reference for isinstance checks.
_LLM_ROUTE_SENTINEL = LlmPauseResult


# ---------------------------------------------------------------------------
# Node status — canonical definition lives in common/schema/node_status.py
# ---------------------------------------------------------------------------

from metagpt.common.schema.node_status import BgStatus  # noqa: E402


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
