"""Typed execution graph primitives for agent runs.

The graph owns control flow only. Domain state remains in flow services,
session store, output engine, and effect ledger; this module deliberately has
no dependency on Role or any concrete agent subsystem.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Generic, Protocol, TypeVar


class NodeId(str, Enum):
    """Closed set of states in the built-in agent execution graph."""

    RESTORE = "restore"
    OBSERVE = "observe"
    BUDGET = "budget"
    THINK = "think"
    INTERPRET = "interpret"
    ACT = "act"
    VALIDATE_OUTPUT = "validate_output"
    AWAIT_QUIESCENCE = "await_quiescence"


class EffectKind(str, Enum):
    """Recovery semantics of a flow node."""

    PURE = "pure"
    REPLAYABLE = "replayable"
    LEDGERED = "ledgered"
    EXTERNAL = "external"
    WAITABLE = "waitable"


@dataclass(frozen=True)
class Transition:
    """A validated request to continue execution at ``target``."""

    target: NodeId


ResultT = TypeVar("ResultT")


@dataclass(frozen=True)
class End(Generic[ResultT]):
    """Terminal graph transition carrying the flow result."""

    result: ResultT


StateT = TypeVar("StateT")
StateT_contra = TypeVar("StateT_contra", contravariant=True)


class AgentNode(Protocol[StateT_contra, ResultT]):
    """One typed state in an agent graph."""

    node_id: NodeId
    effect_kind: EffectKind
    allowed_targets: frozenset[NodeId]

    async def run(self, state: StateT_contra) -> Transition | End[ResultT]: ...


class GraphStructureError(ValueError):
    """Raised when a graph contains a missing or illegal edge."""


class GraphStepLimitError(RuntimeError):
    """Raised when a graph fails to reach a terminal state within its bound."""


class AgentGraph(Generic[StateT, ResultT]):
    """Immutable, structure-validated graph definition."""

    def __init__(
        self,
        *,
        start: NodeId,
        nodes: Mapping[NodeId, AgentNode[StateT, ResultT]],
    ) -> None:
        self.start = start
        self.nodes = dict(nodes)
        self._validate()

    def _validate(self) -> None:
        if self.start not in self.nodes:
            raise GraphStructureError(f"start node is not registered: {self.start.value}")
        for node_id, node in self.nodes.items():
            if node.node_id is not node_id:
                raise GraphStructureError(f"node key {node_id.value!r} does not match node id {node.node_id.value!r}")
            if not isinstance(getattr(node, "effect_kind", None), EffectKind):
                raise GraphStructureError(f"node {node_id.value!r} has no valid effect classification")
            missing = node.allowed_targets.difference(self.nodes)
            if missing:
                rendered = ", ".join(sorted(target.value for target in missing))
                raise GraphStructureError(f"node {node_id.value!r} targets missing nodes: {rendered}")


class GraphRunner(Generic[StateT, ResultT]):
    """Execute validated transitions until a node returns :class:`End`.

    ``on_transition`` is an observation seam. It cannot modify state or choose
    the next node, keeping graph control deterministic and independently
    testable.
    """

    def __init__(
        self,
        graph: AgentGraph[StateT, ResultT],
        *,
        max_steps: int = 100_000,
        on_transition: Callable[[NodeId, NodeId], Awaitable[None]] | None = None,
        execute_node: (
            Callable[
                [AgentNode[StateT, ResultT], StateT],
                Awaitable[Transition | End[ResultT]],
            ]
            | None
        ) = None,
    ) -> None:
        if max_steps < 1:
            raise ValueError("max_steps must be positive")
        self._graph = graph
        self._max_steps = max_steps
        self._on_transition = on_transition
        self._execute_node = execute_node

    async def run(self, state: StateT) -> ResultT:
        current = self._graph.start
        execute_node = self._execute_node
        for _ in range(self._max_steps):
            node = self._graph.nodes[current]
            outcome = await execute_node(node, state) if execute_node is not None else await node.run(state)
            if isinstance(outcome, End):
                return outcome.result
            if outcome.target not in node.allowed_targets:
                raise GraphStructureError(f"illegal transition {current.value!r} -> {outcome.target.value!r}")
            if self._on_transition is not None:
                await self._on_transition(current, outcome.target)
            current = outcome.target
        raise GraphStepLimitError(f"agent graph exceeded {self._max_steps} transitions")


__all__ = [
    "AgentGraph",
    "AgentNode",
    "End",
    "EffectKind",
    "GraphRunner",
    "GraphStepLimitError",
    "GraphStructureError",
    "NodeId",
    "Transition",
]
