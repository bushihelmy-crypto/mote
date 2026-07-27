"""Strongly typed, immutable context values for one Agent run and tool call."""
from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Callable, Generic, Mapping, TypeVar

DepsT = TypeVar("DepsT")
ToolDepsT = TypeVar("ToolDepsT")


@dataclass(frozen=True, slots=True)
class ToolContext(Generic[ToolDepsT]):
    """The narrow dependency projection granted to one tool invocation.

    ``deps`` is intentionally not required to equal the Agent's full dependency
    type. A :class:`RunContext` creates this value only through an explicit
    projector, preserving Mote's least-privilege tool boundary.
    """

    deps: ToolDepsT
    session_id: str
    run_id: str
    tool_call_id: str = ""
    metadata: Mapping[str, object] | None = None

    def __post_init__(self) -> None:
        if self.metadata is not None:
            object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))


@dataclass(frozen=True, slots=True)
class RunContext(Generic[DepsT]):
    """Dependencies and stable identity for one committed-output run attempt."""

    deps: DepsT
    session_id: str
    run_id: str
    metadata: Mapping[str, object] | None = None

    def __post_init__(self) -> None:
        if self.metadata is not None:
            object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))

    def for_tool(
        self,
        project: Callable[[DepsT], ToolDepsT],
        *,
        tool_call_id: str = "",
    ) -> ToolContext[ToolDepsT]:
        """Project full run dependencies onto a narrower tool-specific view."""

        return ToolContext(
            deps=project(self.deps),
            session_id=self.session_id,
            run_id=self.run_id,
            tool_call_id=tool_call_id,
            metadata=self.metadata,
        )


__all__ = ["RunContext", "ToolContext"]
