#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
ToolExecutor — unified command dispatch & execution engine.

Separates "what to execute" (Role._inference) from "how to execute"
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
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Generic, Mapping, TypeVar

from mote.contracts.config.tool import DurableConfig, LoopGuardConfig, RunJournalConfig, ToolResultLimitConfig
from mote.contracts.foundation.errors.codes import RecoveryAction
from mote.contracts.ports.tool.deferred import DeferredResultProjector
from mote.contracts.ports.tool.policy import ToolCallPolicy, ToolResultPolicy
from mote.contracts.tool import (
    CommandProtocol,
    ToolAttemptOrdinal,
    ToolEffect,
    ToolInvocationId,
    ToolInvocationIdentity,
    serialize_tool_call_args,
    tool_arguments_digest,
)
from mote.contracts.tool.errors import ToolNotFoundError
from mote.kernel.execution.run_context import RunContext
from mote.runtime.config.mcp import MCPServerConfig
from mote.runtime.events.telemetry import TelemetryManifest, TelemetryRuntime
from mote.runtime.ledger import RunJournal
from mote.runtime.resilience.recovery import RecoveryRunner, RecoveryStrategy
from mote.runtime.resources import spill as tool_result_limit
from mote.runtime.run_context import current_run_context
from mote.runtime.session.workspace import SessionWorkspace
from mote.runtime.telemetry.logging import log_class
from mote.runtime.tools.base_executor import BaseToolExecutor
from mote.runtime.tools.base_tool import BaseTool, ToolCapabilityProvider
from mote.runtime.tools.mcp.lifecycle import McpLifecycle
from mote.runtime.tools.policy import build_tool_call_policy, build_tool_result_policy
from mote.runtime.tools.provider import NativeToolset, XmlToolset, validate_toolset_protocols
from mote.runtime.tools.provider_definitions import NativeToolDefinition, XmlToolDefinition
from mote.runtime.tools.tool_binding import ExecutableToolBinding
from mote.runtime.tools.tool_catalog import NativeToolCatalog, XmlToolCatalog
from mote.runtime.tools.tool_lifecycle import ToolLifecycle
from mote.runtime.tools.tool_pipeline import ToolExecutionPipeline, failed_result
from mote.runtime.tools.tool_result import ToolResult
from mote.runtime.tools.tool_settlement import ToolSettlement
from mote.runtime.tools.tool_views import ToolExecutorViews

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

AgentDepsT = TypeVar("AgentDepsT")


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
class ToolExecutor(ToolExecutorViews, BaseToolExecutor, Generic[AgentDepsT]):
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
        role: ToolCapabilityProvider | None = None,
        limit_config: ToolResultLimitConfig | None = None,
        journal_config: RunJournalConfig | None = None,
        durable_config: DurableConfig | None = None,
        tool_call_policy: ToolCallPolicy | None = None,
        tool_result_policy: ToolResultPolicy | None = None,
        loop_guard_config: LoopGuardConfig | None = None,
        telemetry: TelemetryRuntime | None = None,
        recovery_strategies: Mapping[RecoveryAction, RecoveryStrategy] | None = None,
        deferred_result_projector: DeferredResultProjector | None = None,
        pipelines_enabled: bool = True,
        workspace_store: SessionWorkspace | None = None,
        deferred_tools: set[str] | None = None,
        get_revealed: Callable[[], set[str]] | None = None,
        toolsets: tuple[XmlToolset[AgentDepsT] | NativeToolset[AgentDepsT], ...] | None = None,
        command_protocol: str | CommandProtocol = CommandProtocol.NATIVE,
        mcp_servers: list[MCPServerConfig] | None = None,
        oauth_root: Path | None = None,
    ) -> None:
        self._session_id = session_id
        if workspace_store is None:
            raise ValueError("ToolExecutor requires an explicit workspace_store")
        self._workspace_store = workspace_store
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
        self._attempt_ordinals: dict[ToolInvocationId, int] = {}
        self._mcp_lifecycle = McpLifecycle(
            servers=mcp_servers,
            oauth_root=oauth_root,
        )
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
        self._journal_config = journal_config or RunJournalConfig()
        self._durable_config = durable_config or DurableConfig()
        self._journal: RunJournal | None = (
            RunJournal(session_id, store=self._workspace_store)
            if (self._journal_config.enabled or self._durable_config.enabled)
            else None
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
            journal=self._journal if self._journal_config.enabled else None,
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
            journal=self._journal if self._journal_config.enabled else None,
            recovery_runner=self._recovery_runner,
            deferred_projector=deferred_result_projector,
            settlement=self._settlement,
        )
        self._deferred_result_projector = deferred_result_projector

    def prepare(self) -> None:
        self._lifecycle.prepare()

    async def start_run(self, ctx: RunContext[AgentDepsT]) -> None:
        await self._lifecycle.start_run(ctx)
        if self._deferred_result_projector is not None:
            self._deferred_result_projector.activate()

    async def prepare_run_step(self, ctx: RunContext[AgentDepsT]) -> None:
        await self._lifecycle.prepare_run_step(ctx)

    async def end_run(self) -> None:
        try:
            await self._lifecycle.end_run()
        finally:
            if self._deferred_result_projector is not None:
                self._deferred_result_projector.deactivate()

    @property
    def _bound_catalog(self):
        return self._catalog

    @property
    def limit_config(self) -> ToolResultLimitConfig:
        return self._limit_config

    @property
    def journal_config(self) -> RunJournalConfig:
        return self._journal_config

    @property
    def durable_config(self) -> DurableConfig:
        return self._durable_config

    @property
    def journal(self) -> RunJournal | None:
        return self._journal

    @property
    def command_protocol(self) -> CommandProtocol:
        return self._command_protocol

    def register_native_tool(self, definition: NativeToolDefinition[Any], capability: BaseTool) -> None:
        """Register one runtime-discovered Native definition and capability."""

        if self._command_protocol is not CommandProtocol.NATIVE:
            raise TypeError("runtime-discovered Native tool cannot be registered on an XML executor")
        self._lifecycle.register_native(definition, capability)

    def register_xml_tool(self, definition: XmlToolDefinition[Any], capability: BaseTool) -> None:
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
        """Package-internal live map used by executor lifecycle mechanics."""
        self.prepare()
        return self._catalog._live_tools()

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
        invocation_id = ToolInvocationId(result_id or f"tool-{uuid.uuid4().hex}")
        attempt = self._attempt_ordinals.get(invocation_id, 0) + 1
        self._attempt_ordinals[invocation_id] = attempt
        bound = self._catalog.get(name)
        definition_identity = (
            bound.semantic_identity
            if isinstance(bound, ExecutableToolBinding)
            else f"mote.tool-definition.missing/v1:{tool_arguments_digest({'name': name})}"
        )
        run_context = current_run_context()
        identity = ToolInvocationIdentity(
            invocation_id=invocation_id,
            attempt_ordinal=ToolAttemptOrdinal(attempt),
            definition_identity=definition_identity,
            catalog_generation=self._catalog.generation,
            arguments_digest=tool_arguments_digest(kwargs or {}),
            owner_id=self._session_id,
            run_id=run_context.run_id if run_context is not None else "",
        )
        if self._catalog.is_hidden(name):
            return failed_result(
                ToolNotFoundError(
                    f"deferred tool '{name}' is not enabled. Call "
                    f"SearchTools(query='{name}') first, then retry it on the next turn."
                )
            )
        return await self._pipeline.run(name, kwargs or {}, identity)

    async def run_pinned_command(
        self,
        binding: ExecutableToolBinding,
        name: str,
        kwargs: dict[str, Any],
        *,
        catalog_generation: int,
        result_id: str | None = None,
    ) -> ToolResult:
        """Execute the exact immutable binding retained by a snapshot revision."""

        invocation_id = ToolInvocationId(result_id or f"tool-{uuid.uuid4().hex}")
        attempt = self._attempt_ordinals.get(invocation_id, 0) + 1
        self._attempt_ordinals[invocation_id] = attempt
        run_context = current_run_context()
        identity = ToolInvocationIdentity(
            invocation_id=invocation_id,
            attempt_ordinal=ToolAttemptOrdinal(attempt),
            definition_identity=binding.semantic_identity,
            catalog_generation=catalog_generation,
            arguments_digest=tool_arguments_digest(kwargs),
            owner_id=self._session_id,
            run_id=run_context.run_id if run_context is not None else "",
        )
        return await self._pipeline.run(name, kwargs, identity, binding=binding)

    def will_ledger(self, name: str, args: dict[str, Any], result_id: str | None) -> bool:
        tool = self._get_tool(name)
        return (
            tool is not None
            and self._journal is not None
            and self._journal_config.enabled
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
        if self._deferred_result_projector is not None:
            await self._deferred_result_projector.aclose()
