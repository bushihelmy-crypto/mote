"""Static/dynamic tool catalog activation and teardown lifecycle."""

from __future__ import annotations

import asyncio
import inspect
from contextlib import AsyncExitStack
from typing import Any, Generic, TypeAlias, TypeVar

from mote.contracts.events.tool import ToolsChangedEvent
from mote.contracts.tool import CommandProtocol
from mote.kernel.execution.run_context import RunContext
from mote.runtime.tools.base_tool import BaseTool, ToolCapabilityProvider
from mote.runtime.tools.mcp.lifecycle import McpCandidate, McpLifecycle
from mote.runtime.tools.provider import (
    NativeToolset,
    ToolsetCompositionError,
    XmlToolset,
    materialize_toolset_index,
    validate_toolset_protocols,
)
from mote.runtime.tools.provider_definitions import NativeToolDefinition, XmlToolDefinition
from mote.runtime.tools.tool_binding import ExecutableToolBinding
from mote.runtime.tools.tool_catalog import BoundToolCatalog
from mote.runtime.tools.tool_settlement import ToolSettlement

AgentDepsT = TypeVar("AgentDepsT")
TypedToolset: TypeAlias = XmlToolset[AgentDepsT] | NativeToolset[AgentDepsT]


class ToolLifecycle(Generic[AgentDepsT]):
    def __init__(
        self,
        *,
        session_id: str,
        declared_tools: tuple[str, ...],
        role: ToolCapabilityProvider | None,
        pipelines_enabled: bool,
        catalog: BoundToolCatalog,
        mcp_lifecycle: McpLifecycle,
        settlement: ToolSettlement,
        toolsets: tuple[TypedToolset[AgentDepsT], ...],
        command_protocol: CommandProtocol,
    ) -> None:
        self._session_id = session_id
        self._declared_tools = declared_tools
        self._role = role
        self._pipelines_enabled = pipelines_enabled
        self._catalog = catalog
        self._mcp_lifecycle = mcp_lifecycle
        self._settlement = settlement
        self._configured_toolsets = toolsets
        self._toolsets = toolsets
        self._command_protocol = command_protocol
        self._prepared = False
        self._preparing = False
        self._run_active = False
        self._run_requires_reset = False
        self._run_exit_stack: AsyncExitStack | None = None

    def prepare(self) -> None:
        if self._prepared or self._preparing:
            return
        self._preparing = True
        try:
            definitions_by_name = materialize_toolset_index(
                self._command_protocol,
                self._toolsets,
            )
            if self._declared_tools:
                bound: dict[object, Any] = {}
                presented: dict[tuple[int, object, str], ExecutableToolBinding] = {}
                skipped: set[object] = set()
                for name in self._declared_tools:
                    resolved = definitions_by_name.get(name)
                    if resolved is None:
                        continue
                    toolset, definition = resolved
                    factory = definition.capability_factory
                    if factory in skipped:
                        continue
                    if factory not in bound:
                        instance = factory()
                        instance.bind(self._session_id, role=self._role)
                        if not self._pipelines_enabled and definition.execution_kind.is_workflow:
                            skipped.add(factory)
                            continue
                        bound[factory] = instance
                    names = list(definition.names)
                    if not names:
                        continue
                    presentation_key = (id(toolset), factory, definition.name)
                    if presentation_key not in presented:
                        presented[presentation_key] = ExecutableToolBinding(
                            definition,
                            bound[factory],
                            toolset.bind_approval(definition),
                        )
                    self._catalog.register(presented[presentation_key], names)
            self._prepared = True
        finally:
            self._preparing = False

    async def start_run(self, ctx: RunContext[AgentDepsT]) -> None:
        """Activate per-run Toolset views and enter their owned resources."""

        if self._run_active:
            raise RuntimeError("Toolset run lifecycle is already active")
        active = tuple(await asyncio.gather(*(toolset.for_run(ctx) for toolset in self._configured_toolsets)))
        validate_toolset_protocols(self._command_protocol, active)
        changed = any(current is not configured for current, configured in zip(active, self._configured_toolsets))
        scoped = any(toolset.run_scoped_lifecycle for toolset in active)
        stack = AsyncExitStack()
        await stack.__aenter__()
        try:
            for toolset in active:
                if toolset.run_scoped_lifecycle:
                    await stack.enter_async_context(toolset)
            if changed or scoped or any(toolset.requires_permission_gate for toolset in active):
                await self._replace_declared_toolsets(active)
        except BaseException:
            await stack.aclose()
            await self._restore_configured_toolsets()
            raise
        self._toolsets = active
        self._run_active = True
        self._run_requires_reset = changed or scoped or any(toolset.requires_permission_gate for toolset in active)
        self._run_exit_stack = stack

    async def prepare_run_step(self, ctx: RunContext[AgentDepsT]) -> None:
        """Refresh per-step dynamic Toolsets before prompt/spec projection."""

        if not self._run_active:
            return
        if not any(toolset.changes_per_run_step for toolset in self._toolsets):
            return
        refreshed = tuple(await asyncio.gather(*(toolset.for_run_step(ctx) for toolset in self._toolsets)))
        validate_toolset_protocols(self._command_protocol, refreshed)
        if any(current is not previous for current, previous in zip(refreshed, self._toolsets)):
            raise ToolsetCompositionError("per-step Toolset refresh must update its run-owned instance in place")
        await self._replace_declared_toolsets(refreshed)

    async def end_run(self) -> None:
        """Release run-owned Toolsets without touching the MCP connection owner."""

        if not self._run_active:
            return
        stack, self._run_exit_stack = self._run_exit_stack, None
        requires_reset = self._run_requires_reset
        self._run_active = False
        self._run_requires_reset = False
        try:
            if requires_reset:
                await self._clear_declared_tools()
                self._toolsets = self._configured_toolsets
                self._prepared = False
        finally:
            if stack is not None:
                await stack.aclose()

    async def _replace_declared_toolsets(self, toolsets: tuple[TypedToolset[AgentDepsT], ...]) -> None:
        await self._clear_declared_tools()
        self._toolsets = toolsets
        self._prepared = False
        self.prepare()

    async def _restore_configured_toolsets(self) -> None:
        try:
            await self._clear_declared_tools()
            self._toolsets = self._configured_toolsets
            self._prepared = False
            self.prepare()
        except Exception:
            return

    async def _clear_declared_tools(self) -> None:
        names: list[str] = []
        seen_ids: set[int] = set()
        for tool in tuple(self._catalog.iter_unique()):
            if self._catalog.category(tool) == "mcp":
                continue
            names.extend(self._catalog.names_for(tool))
            if id(tool) in seen_ids:
                continue
            seen_ids.add(id(tool))
            await self._cleanup_tool_session(
                tool,
                getattr(tool, "name", type(tool).__name__),
            )
        if names:
            self._catalog.remove(names)

    def register_native(self, definition: NativeToolDefinition[Any], capability: BaseTool) -> None:
        self.prepare()
        capability.bind(self._session_id, role=self._role)
        bound = ExecutableToolBinding(definition, capability)
        self._catalog.register(bound, list(definition.names))

    def static_toolset_instructions(self) -> tuple[str, ...]:
        return tuple(
            dict.fromkeys(block for toolset in self._configured_toolsets for block in toolset.static_instruction_blocks)
        )

    def dynamic_toolset_instructions(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(block for toolset in self._toolsets for block in toolset.dynamic_instruction_blocks))

    def register_xml(self, definition: XmlToolDefinition[Any], capability: BaseTool) -> None:
        self.prepare()
        capability.bind(self._session_id, role=self._role)
        bound = ExecutableToolBinding(definition, capability)
        self._catalog.register(bound, list(definition.names))

    async def deregister(self, name: str) -> bool:
        self.prepare()
        tool = self._catalog.get(name)
        if tool is None:
            return False
        removed = self._catalog.names_for(tool)
        await self._cleanup_tool_session(tool, name)
        self._catalog.remove(removed)
        await self._announce(removed, f"ToolsChangedEvent for {name} not delivered")
        return True

    async def init_mcp(self, executor: object, mcps: list[str] | None, *, enabled: bool) -> None:
        if enabled and not self._mcp_lifecycle.active:
            await self._replace_mcp(mcps or None, announce=False)

    async def reload_mcp(self, executor: object, mcps: list[str] | None, *, enabled: bool) -> bool:
        if not enabled:
            return False
        await self._replace_mcp(mcps or None, announce=True)
        return True

    async def _prepare_mcp(self, mcps: list[str] | None) -> McpCandidate:
        if self._command_protocol is CommandProtocol.XML:
            return await self._mcp_lifecycle.prepare_xml(mcps)
        return await self._mcp_lifecycle.prepare_native(mcps)

    async def _replace_mcp(self, mcps: list[str] | None, *, announce: bool) -> None:
        """Prepare a complete generation, then publish it in one catalog swap."""

        self.prepare()
        candidate = await self._prepare_mcp(mcps)
        bound: list[ExecutableToolBinding] = []
        try:
            for definition, capability in candidate.bindings:
                capability.bind(self._session_id, role=self._role)
                bound.append(ExecutableToolBinding(definition, capability))
            new_names = tuple(name for tool in bound for name in tool.definition.names)
            old_name_set = set(self._catalog.mcp_names())
            new_name_set = set(new_names)
            old_tools, old_names = self._catalog.replace_mcp(
                tuple((tool, tuple(tool.definition.names)) for tool in bound)
            )
            previous_owner = self._mcp_lifecycle.activate(candidate)
        except BaseException:
            for tool in bound:
                try:
                    await self._cleanup_tool_session(tool, tool.definition.name)
                except Exception:
                    pass
            await self._mcp_lifecycle.discard(candidate)
            raise

        cleanup_failures: list[tuple[str, BaseException]] = []
        for tool in old_tools:
            try:
                await self._cleanup_tool_session(tool, getattr(tool, "name", type(tool).__name__))
            except Exception as exc:
                cleanup_failures.append((getattr(tool, "name", type(tool).__name__), exc))
        try:
            await self._mcp_lifecycle.cleanup_owner(previous_owner)
        except Exception as exc:
            cleanup_failures.append(("mcp-owner", exc))
        if announce:
            await self._announce(
                sorted(old_name_set - new_name_set),
                "ToolsChangedEvent after MCP reload not delivered",
                added=sorted(new_name_set - old_name_set),
                changed=sorted(new_name_set & old_name_set),
            )
        if cleanup_failures:
            details = "; ".join(f"{name}: {type(exc).__name__}: {exc}" for name, exc in cleanup_failures)
            raise RuntimeError(f"MCP generation committed but prior generation cleanup failed: {details}")

    async def _announce(
        self,
        removed: list[str],
        context: str,
        *,
        added: list[str] | None = None,
        changed: list[str] | None = None,
    ) -> None:
        await self._settlement.observe(
            ToolsChangedEvent(
                removed=removed,
                added=added or [],
                changed=changed or [],
                generation=self._catalog.generation,
                reconstructable=sorted(self._catalog.reconstructable_names()),
            ),
            context=context,
        )

    async def _cleanup_tool_session(self, tool: Any, name: str) -> None:
        result = tool.cleanup_session(self._session_id)
        if inspect.isawaitable(result):
            await result

    async def cleanup(self) -> None:
        failures: list[tuple[str, BaseException]] = []
        try:
            await self.end_run()
        except Exception as exc:
            failures.append(("run-toolsets", exc))
        self.prepare()
        seen_ids: set[int] = set()
        for tool in tuple(self._catalog.iter_unique()):
            if id(tool) not in seen_ids:
                seen_ids.add(id(tool))
                name = getattr(tool, "name", type(tool).__name__)
                try:
                    await self._cleanup_tool_session(tool, name)
                except Exception as exc:
                    failures.append((name, exc))
                else:
                    self._catalog.remove(self._catalog.names_for(tool))
        try:
            await self._mcp_lifecycle.teardown()
        except Exception as exc:
            failures.append(("mcp", exc))
        if failures:
            details = "; ".join(f"{name}: {type(exc).__name__}: {exc}" for name, exc in failures)
            raise RuntimeError(f"Tool lifecycle shutdown failed: {details}")

    @property
    def mcp(self):
        return self._mcp_lifecycle.mcp
