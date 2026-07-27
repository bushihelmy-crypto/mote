#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
ToolExecutor — unified command dispatch & execution engine.

Separates "what to execute" (Role._think) from "how to execute"
(ToolExecutor.run_command).

Design:
- All tools are BaseTool instances, resolved from the tool registry.
- Tools declare needed Role capabilities via `requires`; bind() injects only
  those narrow methods (never RoleState or memory).
- Dynamic tools (MCP, etc.) are wrapped as BaseTool adapters and share the
  same single dispatch path.
- No special-cased commands — everything is a tool.
"""
from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any, Callable, Mapping

from mote.contracts.ports.tool_policy import ToolCallPolicy, ToolResultPolicy
from mote.contracts.run_context import RunContext
from mote.contracts.schema import DurableConfig, EffectLedgerConfig, LoopGuardConfig, ToolResultLimitConfig
from mote.contracts.tools import CommandProtocol, ToolEffect, serialize_tool_call_args
from mote.kernel.tools.toolset import AnyToolset, validate_toolset_protocols
from mote.runtime.errors import RecoveryAction, RecoveryRunner, RecoveryStrategy, ToolNotFoundError
from mote.runtime.events.telemetry import TelemetryManifest, TelemetryRuntime
from mote.runtime.ledger import RunJournal
from mote.runtime.logging import log_class
from mote.runtime.tools import tool_result_limit
from mote.runtime.tools.base_executor import BaseToolExecutor
from mote.runtime.tools.effect_ledger import EffectLedger
from mote.runtime.tools.mcp.lifecycle import McpLifecycle
from mote.runtime.tools.policy import build_tool_call_policy, build_tool_result_policy
from mote.runtime.tools.tool_catalog import NativeToolCatalog, XmlToolCatalog
from mote.runtime.tools.tool_lifecycle import ToolLifecycle
from mote.runtime.tools.tool_pipeline import ToolExecutionPipeline, failed_result
from mote.runtime.tools.tool_result import ToolResult
from mote.runtime.tools.tool_settlement import ToolSettlement
from mote.runtime.tools.tool_views import ToolExecutorViews
from mote.runtime.workspace import WorkspaceStore

if TYPE_CHECKING:
    from mote.runtime.tools.mcp.universal import UniversalMCP


# Refusal shown when a resumed session re-dispatches an EXTERNAL call that the
# ledger last saw as ``started`` — its outcome was lost to a crash, so re-running
# it might duplicate a side effect. The framework cannot know whether the effect
# took hold; that judgment (verify / retry / abandon) is left to the model.
_UNKNOWN_AFTER_CRASH = (
    "<unknown-after-crash>\n"
    "Tool '{name}' (call {call_id}) was started before a restart but its outcome "
    "was never recorded, so re-running it could duplicate an external side effect. "
    "It was NOT re-run. Verify whether the effect already took hold; reissue the "
    "call only if it is safe to retry."
    "\n</unknown-after-crash>"
)


# ---------------------------------------------------------------------------
# ToolExecutor — dispatch engine
# ---------------------------------------------------------------------------


@log_class(
    level="DEBUG",
    # Schema introspection getters are pure/derived and called frequently when
    # building prompts — tracing them only adds noise.
    exclude={
        "xml_tool_schemas",
        "mcp_tool_schemas",
        "xml_pipeline_tool_schemas",
        "all_xml_tool_schemas",
        "native_tool_specs",
    },
)
class ToolExecutor(ToolExecutorViews, BaseToolExecutor):
    """Dispatch LLM tool calls to BaseTool instances.

    Lifecycle:
        1. Role creates ToolExecutor with session_id and declared tools list.
        2. Constructor pre-binds static tools from the tool registry.
        3. Dynamic MCP capabilities are projected through an explicit XML or
           Native MCP Toolset and registered through the matching boundary.
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
        ledger_config: EffectLedgerConfig | None = None,
        durable_config: DurableConfig | None = None,
        tool_call_policy: ToolCallPolicy | None = None,
        tool_result_policy: ToolResultPolicy | None = None,
        loop_guard_config: LoopGuardConfig | None = None,
        telemetry: TelemetryRuntime | None = None,
        recovery_strategies: Mapping[RecoveryAction, RecoveryStrategy] | None = None,
        get_bg_pool: Callable[[], Any] | None = None,
        pipelines_enabled: bool = True,
        workspace_store: WorkspaceStore | None = None,
        deferred_tools: set[str] | None = None,
        get_revealed: Callable[[], set[str]] | None = None,
        toolsets: tuple[AnyToolset, ...] | None = None,
        command_protocol: str | CommandProtocol = CommandProtocol.NATIVE,
    ) -> None:
        self._session_id = session_id
        # Workspace layout owner used to place a large persisted tool result
        # under this session's directory. Defaults to the standard workspace
        # root; a shared instance can be injected via the component graph.
        self._workspace_store = workspace_store or WorkspaceStore()
        # Two collaborators carry the split state: the catalog owns the bound-tool
        # map + schema views, the lifecycle owns the MCP slot. Tool-search
        # deferral: the catalog hides deferred tools' full descriptions until
        # they are revealed (read live via ``get_revealed`` — the revealed set
        # lives on RoleState so it survives session resume). Dispatch applies the
        # same reveal gate so a guessed deferred name cannot bypass SearchTools.
        self._command_protocol = CommandProtocol(command_protocol)
        self._toolsets = toolsets if toolsets is not None else ()
        validate_toolset_protocols(self._command_protocol, self._toolsets)
        catalog_type = XmlToolCatalog if self._command_protocol is CommandProtocol.XML else NativeToolCatalog
        self._catalog = catalog_type(deferred=deferred_tools, get_revealed=get_revealed)
        self._mcp_lifecycle = McpLifecycle()
        self._get_bg_pool = get_bg_pool
        # Telemetry carries post-operation observations and settlement
        # policy. Pre-invocation control belongs exclusively to ToolCallPolicy.
        self._telemetry = telemetry or TelemetryRuntime(TelemetryManifest(()))
        # Tool-level failover skeleton. The same domain-agnostic loop the LLM
        # layer uses (read ``exc.recovery`` → dispatch an injected strategy →
        # retry). The registry is EMPTY by default, so the runner is
        # behaviourally identical to an un-wrapped ``tool.call()``: a typed
        # ``ToolError`` (ABORT) or ``RetryableToolError`` (RETRY) is re-raised
        # straight back to the try/except in run_command. Future tool-level
        # recovery strategies (e.g. COMPRESS an oversized tool result) plug in
        # here via ``recovery_strategies`` with no further wiring.
        self._recovery_runner = RecoveryRunner(recovery_strategies or {})
        # Tool-result size limiting knobs (per-tool cap + disk persistence). A
        # default config reproduces the out-of-the-box behavior.
        self._limit_config = limit_config or ToolResultLimitConfig()

        # EXTERNAL-effect idempotency ledger (crash-replay guard). The executor
        # is the single owner of this cross-cutting policy (mirrors limit_config).
        # Built once per session, co-located under the session directory via the
        # shared workspace store; ``None`` when disabled → run_command skips all
        # ledger work (identical to the prior no-ledger behavior).
        self._ledger_config = ledger_config or EffectLedgerConfig()
        self._durable_config = durable_config or DurableConfig()
        self._journal: RunJournal | None = (
            RunJournal(session_id, store=self._workspace_store)
            if (self._ledger_config.enabled or self._durable_config.enabled)
            else None
        )
        self._ledger: EffectLedger | None = (
            EffectLedger(journal=self._journal) if self._ledger_config.enabled and self._journal is not None else None
        )

        # A standalone executor is its own composition root.  The Role path
        # injects a policy assembled by RoleComponents; standalone approval-
        # required Toolsets still force the core gate on so composition cannot
        # bypass their declared boundary.
        toolset_requires_gate = any(
            bool(getattr(toolset, "requires_permission_gate", False)) for toolset in self._toolsets
        )
        self._tool_call_policy = tool_call_policy or build_tool_call_policy(
            None,
            role=role,
            require_permission=toolset_requires_gate,
        )

        self._tool_result_policy = tool_result_policy or build_tool_result_policy(
            loop_guard_config=loop_guard_config,
        )
        self._settlement = ToolSettlement(
            session_id=self._session_id,
            telemetry=self._telemetry,
            get_tool=self._get_tool,
            ledger=self._ledger,
            limit_config=self._limit_config,
            workspace_store=self._workspace_store,
            policy=self._tool_result_policy,
        )
        self._lifecycle = ToolLifecycle(
            session_id=self._session_id,
            declared_tools=tuple(tools or ()),
            role=role,
            pipelines_enabled=pipelines_enabled,
            catalog=self._catalog,
            mcp_lifecycle=self._mcp_lifecycle,
            settlement=self._settlement,
            toolsets=self._toolsets,
            command_protocol=self._command_protocol,
        )
        self._pipeline = ToolExecutionPipeline(
            get_tool=self._get_tool,
            available_names=self._catalog.names,
            policy=self._tool_call_policy,
            ledger=self._ledger,
            recovery_runner=self._recovery_runner,
            get_bg_pool=self._get_bg_pool,
            settlement=self._settlement,
        )

    def prepare(self) -> None:
        self._lifecycle.prepare()

    async def start_run(self, ctx: RunContext[Any]) -> None:
        await self._lifecycle.start_run(ctx)

    async def prepare_run_step(self, ctx: RunContext[Any]) -> None:
        await self._lifecycle.prepare_run_step(ctx)

    async def end_run(self) -> None:
        await self._lifecycle.end_run()

    @property
    def command_protocol(self) -> CommandProtocol:
        return self._command_protocol

    def register_native_tool(self, definition, capability: Any) -> None:
        """Register one runtime-discovered Native definition and capability."""

        if self._command_protocol is not CommandProtocol.NATIVE:
            raise TypeError("runtime-discovered Native tool cannot be registered on an XML executor")
        self._lifecycle.register_native(definition, capability)

    def register_xml_tool(self, definition, capability: Any) -> None:
        """Register one runtime-discovered XML definition and capability."""

        if self._command_protocol is not CommandProtocol.XML:
            raise TypeError("runtime-discovered XML tool cannot be registered on a Native executor")
        self._lifecycle.register_xml(definition, capability)

    def static_toolset_instructions(self) -> tuple[str, ...]:
        """Session-stable Toolset instructions for the system prompt."""

        return self._lifecycle.static_toolset_instructions()

    def dynamic_toolset_instructions(self) -> tuple[str, ...]:
        """Current run/step Toolset instructions for request-only context."""

        return self._lifecycle.dynamic_toolset_instructions()

    async def deregister_tool(self, name: str) -> bool:
        return await self._lifecycle.deregister(name)

    @property
    def _tools(self) -> dict[str, Any]:
        """The live name→instance map, delegated to the catalog.

        Kept as a read accessor so external introspection (and tests) can do
        ``name in executor._tools`` without reaching into the collaborator.
        """
        self.prepare()
        return self._catalog.tools

    def _get_tool(self, name: str):
        """Resolve a tool by name. Returns the BaseTool instance, or None."""
        self.prepare()
        return self._catalog.get(name)

    def canonical_tool_name(self, name: str) -> str | None:
        tool = self._get_tool(name)
        return (tool.name or name) if tool is not None else None

    def _is_readonly_tool(self, name: str) -> bool:
        tool = self._get_tool(name)
        return tool is not None and tool.resolve_effect() is ToolEffect.PURE

    async def run_command(
        self,
        name: str,
        kwargs: dict[str, Any] | None = None,
        *,
        result_id: str | None = None,
    ) -> ToolResult:
        self.prepare()
        if self._catalog.is_hidden(name):
            return failed_result(
                ToolNotFoundError(
                    f"deferred tool '{name}' is not enabled. Call "
                    f"SearchTools(query='{name}') first, then retry it on the next turn."
                )
            )
        return await self._pipeline.run(name, kwargs or {}, result_id)

    def will_ledger(self, name: str, args: dict[str, Any], result_id: str | None) -> bool:
        tool = self._get_tool(name)
        return (
            tool is not None
            and self._ledger is not None
            and result_id is not None
            and tool.resolve_effect_for(args) is ToolEffect.EXTERNAL
        )

    def persist_large_args(self, args: Any, call_id: str | None) -> Any:
        config = self._limit_config
        if not config.enable_tool_result_limit:
            return args
        serialized = serialize_tool_call_args(args)
        spilled = tool_result_limit.enforce_tool_result_limit(
            serialized,
            "toolcall-args",
            result_id=f"{call_id or uuid.uuid4().hex}-args",
            session_id=self._session_id,
            max_result_size_chars=config.default_max_result_size_chars,
            persist=config.persist_large_tool_results,
            store=self._workspace_store,
        )
        return spilled if spilled != serialized else args

    async def init_mcp(self, mcps: list[str] | None = None, *, enabled: bool = False) -> None:
        await self._lifecycle.init_mcp(self, mcps, enabled=enabled)

    async def reload_mcp(self, mcps: list[str] | None = None, *, enabled: bool = False) -> bool:
        return await self._lifecycle.reload_mcp(self, mcps, enabled=enabled)

    @property
    def mcp(self) -> "UniversalMCP | None":
        return self._lifecycle.mcp

    async def cleanup(self) -> None:
        await self._lifecycle.cleanup()
