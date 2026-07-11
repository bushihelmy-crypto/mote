"""BaseToolExecutor — the act-side orchestration interface.

The Act counterpart to ``BaseThinkEngine`` on the Think side. A tool executor
dispatches one LLM-named command to a bound tool and returns a ``ToolResult``,
and exposes the schema views the prompt builder + native channel consume.
Making it an ABC lets the Role assemble an alternative executor (e.g. a remote
or sandboxed dispatcher) behind the same contract the loop/channel rely on.

The concrete ``ToolExecutor`` keeps owning instance caching, capability binding,
result-size limiting, and MCP lifecycle; those are implementation detail, not
part of this contract.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from mote.executor.tool_result import ToolResult


class BaseToolExecutor(ABC):
    """A replaceable command-dispatch engine.

    Contract used by the react loop, the command channels, and the prompt
    builder. Everything here is what an external collaborator calls; how a tool
    is resolved and run is left to the implementation.
    """

    @abstractmethod
    async def run_command(
        self,
        name: str,
        kwargs: dict[str, Any] | None = None,
        *,
        result_id: str | None = None,
    ) -> ToolResult:
        """Dispatch a single tool call by name and return its ToolResult."""

    @abstractmethod
    def get_native_tool_specs(self, provider: str = "anthropic") -> list[dict]:
        """Native tool-use specs for all declared tools (native channel)."""

    @abstractmethod
    def get_tool_schemas(self) -> dict[str, dict]:
        """Schemas for built-in (non-MCP) tools — primary name -> schema."""

    @abstractmethod
    def get_mcp_tool_schemas(self) -> dict[str, dict]:
        """Schemas for MCP tools — namespaced name -> schema."""
