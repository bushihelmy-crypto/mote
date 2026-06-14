#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
ToolExecutor — unified command dispatch & execution engine.

Extracted from Role to separate "what to execute" (Role._think)
from "how to execute" (ToolExecutor.run_command).

Design:
- All tools are BaseTool instances, resolved from the tool registry.
- Tools declare needed Role capabilities via `requires`; bind() injects only
  those narrow methods (never RoleState or memory).
- Dynamic tools (MCP, etc.) are wrapped as BaseTool adapters and share the
  same single dispatch path.
- No special-cased commands — everything is a tool.
"""
from __future__ import annotations

import traceback
import uuid
from typing import Any

from metagpt.common.schema import DEFAULT_MAX_RESULT_SIZE_CHARS, ToolResultLimitConfig
from executor import tool_result_limit
from metagpt.executor.base_executor import BaseToolExecutor
from metagpt.executor.tool_result import ToolError, ToolResult
from metagpt.executor.tool_registry import registry as tool_registry
from metagpt.common.logs import logger
from metagpt.executor.mcp.universal import UniversalMCP
from metagpt.common.schema import BgTaskResult
from metagpt.executor.tool_spec_adapter import to_native_tool_specs

# ---------------------------------------------------------------------------
# ToolExecutor — dispatch engine
# ---------------------------------------------------------------------------


class ToolExecutor(BaseToolExecutor):
    """Dispatch LLM tool calls to BaseTool instances.

    Lifecycle:
        1. Role creates ToolExecutor with session_id and declared tools list.
        2. Constructor pre-binds static tools from the tool registry.
        3. Dynamic tools (MCP) are wrapped as BaseTool adapters and added later
           via register_tool_instance().
        4. On each LLM tool call, executor dispatches from the single _tools map.

    Instance isolation: each ToolExecutor maintains its own tool instance cache.
    Different Roles never share tool instances — no concurrent bind conflicts.

    Only declared tools are accessible — undeclared tools are invisible to LLM.
    """

    def __init__(
        self,
        session_id: str,
        tools: list[str] | None = None,
        role=None,
        limit_config: ToolResultLimitConfig | None = None,
    ) -> None:
        self._session_id = session_id
        self._mcp: UniversalMCP | None = None
        self._tools: dict[str, Any] = {}  # name -> BaseTool instance (static + dynamic)
        # Tool-result size limiting knobs (per-tool cap + disk persistence). A
        # default config reproduces CC's out-of-the-box behavior.
        self._limit_config = limit_config or ToolResultLimitConfig()

        # Ensure all @register_tool classes under the scanned packages are loaded
        # before we look them up by name. Idempotent — runs the package scan once.
        tool_registry.discover()

        # Pre-bind declared static tools
        if tools:
            bound: dict[type, Any] = {}  # tool_cls -> instance (dedup)
            for name in tools:
                tool_cls = tool_registry.get(name)
                if tool_cls is None:
                    continue
                if tool_cls not in bound:
                    instance = tool_cls()
                    instance.bind(session_id, role=role)
                    bound[tool_cls] = instance
                # Register under all names (primary + aliases)
                for n in tool_registry.all_names(tool_cls):
                    self._tools[n] = bound[tool_cls]

    def register_tool_instance(self, tool: Any, names: list[str]) -> None:
        """Register an already-constructed BaseTool instance under given names.

        Used for runtime-discovered tools (MCP adapters) that aren't in the
        global registry. They share the same _tools map and dispatch path as
        static tools.

        Args:
            tool: A BaseTool instance.
            names: All names (primary + aliases) that route to this instance.
        """
        for name in names:
            self._tools[name] = tool

    def _get_tool(self, name: str):
        """Resolve a tool by name. Returns the BaseTool instance, or None."""
        return self._tools.get(name)

    async def run_command(
        self,
        name: str,
        kwargs: dict[str, Any] | None = None,
        *,
        result_id: str | None = None,
    ) -> ToolResult:
        """Dispatch a single tool call by name.

        Args:
            name: Tool name (primary or alias).
            kwargs: LLM-specified parameters for the tool's call() method.
            result_id: Stable id for this result (the tool-use id). Used to name
                the on-disk file when a large result is persisted, so re-runs
                stay byte-identical. Falls back to a fresh uuid when not given.

        Returns:
            ToolResult with output, success status, and optional structured data.
            If the tool returns a BgTaskResult, it is wrapped in ToolResult.data
            for the caller (Role._act) to handle background submission.

            A large text result is capped per the tool's ``max_result_size_chars``
            (persisted to disk + replaced with a ``<persisted-output>`` preview),
            unless the result carries media or the limit is disabled.
        """
        tool = self._get_tool(name)
        if tool is None:
            available = list(self._tools.keys())
            return ToolResult(output=f"Error: unknown tool '{name}'. Available: {available}", success=False)

        try:
            raw = await tool.call(**(kwargs or {}))
        except ToolError as e:
            # Expected, recoverable failure the tool signalled deliberately.
            # Not logged as an error: it is normal control flow (bad args,
            # missing file, etc.), surfaced to the model as a failed tool result.
            return ToolResult(output=str(e), success=False)
        except Exception as e:
            tb = traceback.format_exc()
            logger.error(f"Tool '{name}' raised: {e}\n{tb}")
            return ToolResult(output=f"Error executing '{name}': {e}", success=False)

        # BgTaskResult: pass through for Role to handle
        if isinstance(raw, BgTaskResult):
            output = str(raw.result) if raw.result else ""
            return ToolResult(output=output, success=True, data=raw)

        # Normalize the raw return into a ToolResult. A returned ToolResult is
        # used as-is; a plain value is always treated as success — failure is
        # signalled structurally (raise ToolError above, or return
        # ToolResult(success=False)), never by sniffing the output text.
        result = ToolResult.from_tool_return(raw)
        return self._limit_result(result, name, result_id)

    def _limit_result(self, result: ToolResult, name: str, result_id: str | None) -> ToolResult:
        """Cap a tool result's text per the tool's declared size limit.

        Mirrors CC's per-tool persistence: when the text output exceeds the
        tool's effective threshold, the full output is written to disk and the
        inline content is replaced by a ``<persisted-output>`` preview. Skipped
        when disabled, when the result carries media (images/PDFs go to the
        model as-is, like CC), or when the output is short.
        """
        cfg = self._limit_config
        if not cfg.enable_tool_result_limit or not result.output:
            return result
        # Media results are sent to the model verbatim (CC skips persistence for
        # image/PDF tool_result blocks).
        if result.images or result.pdfs:
            return result

        tool = self._get_tool(name)
        cap = getattr(tool, "max_result_size_chars", DEFAULT_MAX_RESULT_SIZE_CHARS)
        result.output = tool_result_limit.enforce_tool_result_limit(
            result.output,
            name,
            result_id=result_id or uuid.uuid4().hex,
            session_id=self._session_id,
            max_result_size_chars=cap,
            persist=cfg.persist_large_tool_results,
        )
        return result

    # ------------------------------------------------------------------
    # Schema introspection
    # ------------------------------------------------------------------

    def _is_mcp_tool(self, tool) -> bool:
        """Return True if the tool is a runtime-discovered MCP adapter."""
        from metagpt.executor.mcp_adapter import MCPToolAdapter
        return isinstance(tool, MCPToolAdapter)

    def get_tool_schemas(self) -> dict[str, dict]:
        """Return schemas for built-in (non-MCP) tools only.

        Returns:
            dict mapping primary tool name -> schema dict.
            Deduplicates aliases so each tool appears once.
        """
        schemas: dict[str, dict] = {}
        seen_ids: set[int] = set()
        for tool in self._tools.values():
            if id(tool) in seen_ids:
                continue
            seen_ids.add(id(tool))
            if self._is_mcp_tool(tool):
                continue
            schema = tool.tool_schema()
            schemas[schema["name"]] = schema
        return schemas

    def get_mcp_tool_schemas(self) -> dict[str, dict]:
        """Return schemas for MCP tools only.

        Returns:
            dict mapping namespaced tool name (server:tool) -> schema dict.
            Deduplicates aliases so each tool appears once.
        """
        schemas: dict[str, dict] = {}
        seen_ids: set[int] = set()
        for tool in self._tools.values():
            if id(tool) in seen_ids:
                continue
            seen_ids.add(id(tool))
            if not self._is_mcp_tool(tool):
                continue
            schema = tool.tool_schema()
            schemas[schema["name"]] = schema
        return schemas

    def get_all_tool_schemas(self) -> dict[str, dict]:
        """Return schemas for all declared tools (built-in + MCP).

        Returns:
            dict mapping primary tool name -> schema dict.
            Deduplicates aliases so each tool appears once.
        """
        schemas: dict[str, dict] = {}
        seen_ids: set[int] = set()
        for tool in self._tools.values():
            if id(tool) in seen_ids:
                continue
            seen_ids.add(id(tool))
            schema = tool.tool_schema()
            schemas[schema["name"]] = schema
        return schemas

    def get_native_tool_specs(self, provider: str = "anthropic") -> list[dict]:
        """Return native tool-use specs for all declared tools (static + MCP).

        Each tool contributes a {name, description, input_schema} record (via
        BaseTool.native_schema / MCPToolAdapter.native_schema), wrapped into the
        provider envelope. Deduplicates aliases like get_tool_schemas().

        This is the native-protocol counterpart to get_tool_schemas() and is
        NOT used by the XML path — it exists for the native tool-use channel.
        """


        native: dict[str, dict] = {}
        seen_ids: set[int] = set()
        for tool in self._tools.values():
            if id(tool) in seen_ids:
                continue
            seen_ids.add(id(tool))
            schema = tool.native_schema()
            native[schema["name"]] = schema
        return to_native_tool_specs(native, provider=provider)



    # ------------------------------------------------------------------
    # MCP lifecycle
    # ------------------------------------------------------------------

    async def init_mcp(self, mcps: list[str] | None = None) -> None:
        """Initialize MCP servers and register discovered tools as adapters.

        Args:
            mcps: Server names to initialize (from Role.mcps).
                  If empty/None, no-op.
        """
        if not mcps:
            return
        if self._mcp is not None:
            return  # already initialized
        self._mcp = UniversalMCP()
        await self._mcp.initialize(server_names=mcps)
        self._mcp.register_tools(self)

    @property
    def mcp(self) -> UniversalMCP | None:
        return self._mcp

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    async def cleanup(self) -> None:
        """Clean up all tool sessions and MCP clients. Called when Role exits."""
        seen = set()
        for tool in self._tools.values():
            if id(tool) not in seen:
                seen.add(id(tool))
                tool.cleanup_session(self._session_id)
        self._tools.clear()

        if self._mcp is not None:
            await self._mcp.cleanup_clients()
            self._mcp = None
