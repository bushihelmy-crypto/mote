"""Presentation-neutral contracts for nested execution activity."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from pydantic import JsonValue

from mote.contracts.events.envelope import freeze_json


class ActivityKind(StrEnum):
    GRAPH = "graph"
    AGENT = "agent"
    TASK = "task"


class ActivityNodeKind(StrEnum):
    TOOL = "tool"
    MAP = "map"
    FOLD = "fold"
    COMPUTE = "compute"
    UNSPECIFIED = "unspecified"


class ActivityNodeStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMEOUT = "timeout"
    SKIPPED = "skipped"
    WAITING_FOR_ROUTE = "waiting_for_route"
    STALLED = "stalled"


class ActivityOutcome(StrEnum):
    SUCCESS = "success"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class ActivityNode:
    node_id: str
    kind: ActivityNodeKind
    label: str

    def __post_init__(self) -> None:
        if not isinstance(self.node_id, str) or not self.node_id:
            raise ValueError("activity node_id must be a non-empty string")
        if not isinstance(self.kind, ActivityNodeKind):
            raise TypeError("activity node kind must be ActivityNodeKind")
        if not isinstance(self.label, str):
            raise TypeError("activity node label must be a string")


@dataclass(frozen=True, slots=True)
class ActivityEdge:
    from_node: str
    to_node: str
    guarded: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.from_node, str) or not self.from_node:
            raise ValueError("activity edge from_node must be a non-empty string")
        if not isinstance(self.to_node, str) or not self.to_node:
            raise ValueError("activity edge to_node must be a non-empty string")
        if type(self.guarded) is not bool:
            raise TypeError("activity edge guarded must be a boolean")


@dataclass(frozen=True, slots=True)
class ActivityTopology:
    nodes: tuple[ActivityNode, ...]
    edges: tuple[ActivityEdge, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.nodes, tuple) or not all(isinstance(node, ActivityNode) for node in self.nodes):
            raise TypeError("activity topology nodes must be ActivityNode tuple")
        if not isinstance(self.edges, tuple) or not all(isinstance(edge, ActivityEdge) for edge in self.edges):
            raise TypeError("activity topology edges must be ActivityEdge tuple")
        node_ids = tuple(node.node_id for node in self.nodes)
        if len(node_ids) != len(set(node_ids)):
            raise ValueError("activity topology node ids must be unique")
        known = set(node_ids)
        if any(edge.from_node not in known or edge.to_node not in known for edge in self.edges):
            raise ValueError("activity topology edge references an unknown node")


@dataclass(frozen=True, slots=True)
class ActivityNodeState:
    node_id: str
    kind: ActivityNodeKind
    label: str
    status: ActivityNodeStatus
    attempts: int
    error: str = ""
    arguments: JsonValue | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.node_id, str) or not self.node_id:
            raise ValueError("activity node state node_id must be a non-empty string")
        if not isinstance(self.kind, ActivityNodeKind):
            raise TypeError("activity node state kind must be ActivityNodeKind")
        if not isinstance(self.label, str):
            raise TypeError("activity node state label must be a string")
        if not isinstance(self.status, ActivityNodeStatus):
            raise TypeError("activity node state status must be ActivityNodeStatus")
        if type(self.attempts) is not int:
            raise TypeError("activity node attempts must be an integer")
        if self.attempts < 0:
            raise ValueError("activity node attempts must be non-negative")
        if type(self.error) is not str:
            raise TypeError("activity node error must be a string")
        if self.arguments is not None:
            object.__setattr__(
                self,
                "arguments",
                freeze_json(self.arguments, path="activity node arguments"),
            )


__all__ = [
    "ActivityEdge",
    "ActivityKind",
    "ActivityNode",
    "ActivityNodeKind",
    "ActivityNodeState",
    "ActivityNodeStatus",
    "ActivityOutcome",
    "ActivityTopology",
]
