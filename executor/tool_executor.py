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

import inspect
import uuid
from typing import TYPE_CHECKING, Any, Callable, Mapping

from mote.common.events import EventBus
from mote.common.exception import (
    ErrorReport,
    RecoveryAction,
    RecoveryRunner,
    RecoveryStrategy,
    ToolValidationError,
    render_error_block,
)
from mote.common.ledger import RunJournal
from mote.common.logs import log_class
from mote.common.schema import (
    DurableConfig,
    EffectLedgerConfig,
    LoopGuardConfig,
    PermissionConfig,
    ToolEffect,
    ToolResultLimitConfig,
    serialize_tool_call_args,
)
from mote.common.text import plural
from mote.common.utils.role_utils import call_signature
from mote.common.workspace import WorkspaceStore
from mote.executor import tool_result_limit
from mote.executor.base_executor import BaseToolExecutor
from mote.executor.effect_ledger import EffectLedger
from mote.executor.loop_guard import LoopGuardSubscriber, ThrashDetector
from mote.executor.mcp_lifecycle import McpLifecycle
from mote.executor.permission import PermissionEngine, PermissionSubscriber, RuleStore
from mote.executor.permission.sandbox.guard import SandboxGuard
from mote.executor.tool_catalog import ToolCatalog
from mote.executor.tool_lifecycle import ToolLifecycle
from mote.executor.tool_pipeline import ToolExecutionPipeline
from mote.executor.tool_result import ToolResult
from mote.executor.tool_settlement import ToolSettlement
from mote.executor.tool_views import ToolExecutorViews

if TYPE_CHECKING:
    from mote.executor.mcp.universal import UniversalMCP


def _call_arg_signature(name: str, args: dict[str, Any]) -> str:
    return call_signature([{"name": name, "args": args}])


# Signature params that are framework plumbing, never LLM-facing arguments.
# (``*args``/``**kwargs`` are detected by parameter *kind*, not by name.)
_NON_ARG_PARAMS = frozenset({"self", "cls"})

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


def _validate_call_args(call_fn: Callable, tool_name: str, args: dict[str, Any]) -> None:
    """Pre-flight LLM-supplied args against a tool's ``call()`` signature.

    Raises :class:`ToolValidationError` when a required parameter is missing or
    an unexpected one is supplied, so a malformed call surfaces as a structured
    tool-result failure with a clear "which argument" message instead of an
    opaque ``TypeError`` from ``tool.call(**args)``.

    Skipped entirely when ``call()`` accepts ``**kwargs`` (dynamic / MCP tools,
    whose parameters are not statically known) — this is the natural gate that
    limits validation to tools with a statically-declared signature.

    Type checking is intentionally NOT performed: the XML command
    protocol delivers every argument as a string (e.g. ``timeout="300"``), so
    enforcing declared types would reject valid XML-channel calls; the native
    tool-use channel's API already validates inputs against the JSON schema.
    """
    try:
        sig = inspect.signature(call_fn)
    except (TypeError, ValueError):
        return  # un-introspectable — let tool.call surface any error itself

    known: set[str] = set()
    required: set[str] = set()
    for name, param in sig.parameters.items():
        if param.kind is inspect.Parameter.VAR_KEYWORD:
            return  # dynamic params (**kwargs) — cannot statically validate
        if param.kind is inspect.Parameter.VAR_POSITIONAL or name in _NON_ARG_PARAMS:
            continue
        known.add(name)
        if param.default is inspect.Parameter.empty:
            required.add(name)

    missing = sorted(required - args.keys())
    unexpected = sorted(set(args) - known)
    if not missing and not unexpected:
        return

    parts: list[str] = []
    if missing:
        parts.append(f"missing required {plural('argument', len(missing))}: {', '.join(missing)}")
    if unexpected:
        parts.append(f"unexpected {plural('argument', len(unexpected))}: {', '.join(unexpected)}")
    raise ToolValidationError(f"{tool_name}: {'; '.join(parts)}")


def _failed_result(exc: Exception, *, terminate: bool = False) -> "ToolResult":
    """Normalize a pre-flight failure into a failed ``ToolResult``.

    The pre-flight gates (unknown tool, hook / permission-engine deny) reject a
    call *before* it reaches the recovery loop's try/except, so they would
    otherwise emit ad-hoc ``"Error: …"`` / ``"[PERMISSION DENIED] …"`` strings
    instead of the uniform ``<error>`` block every post-dispatch failure uses.
    Routing them through :class:`ErrorReport` here gives every tool failure —
    pre-flight or in-flight — one shape (rendered block + machine-readable
    ``error`` report on the result).

    ``terminate`` marks the failure as loop-ending (a user rejection / hook veto),
    which the react loop honours by clearing the active signal.
    """
    report = ErrorReport.from_exception(exc)
    return ToolResult(output=render_error_block(report), success=False, error=report, terminate=terminate)


# ---------------------------------------------------------------------------
# ToolExecutor — dispatch engine
# ---------------------------------------------------------------------------


@log_class(
    level="DEBUG",
    # Schema introspection getters are pure/derived and called frequently when
    # building prompts — tracing them only adds noise.
    exclude={
        "get_tool_schemas",
        "get_mcp_tool_schemas",
        "get_pipeline_tool_schemas",
        "get_all_tool_schemas",
        "get_native_tool_specs",
    },
)
class ToolExecutor(ToolExecutorViews, BaseToolExecutor):
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
        ledger_config: EffectLedgerConfig | None = None,
        durable_config: DurableConfig | None = None,
        permission_config: PermissionConfig | None = None,
        loop_guard_config: LoopGuardConfig | None = None,
        bus: EventBus | None = None,
        recovery_strategies: Mapping[RecoveryAction, RecoveryStrategy] | None = None,
        get_bg_pool: Callable[[], Any] | None = None,
        pipelines_enabled: bool = True,
        workspace_store: WorkspaceStore | None = None,
        deferred_tools: set[str] | None = None,
        get_revealed: Callable[[], set[str]] | None = None,
    ) -> None:
        self._session_id = session_id
        # Workspace layout owner used to place a large persisted tool result
        # under this session's directory. Defaults to the standard workspace
        # root; a shared instance can be injected via the component graph.
        self._workspace_store = workspace_store or WorkspaceStore()
        # Two collaborators carry the split state: the catalog owns the bound-tool
        # map + schema views, the lifecycle owns the MCP slot. Tool-search
        # deferral (schema-visibility only): the catalog hides deferred tools'
        # schemas from both channels until they are revealed (read live via
        # ``get_revealed`` — the revealed set lives on RoleState so it survives
        # session resume). Deferral never touches dispatch: a revealed tool is
        # still in the map and resolves through run_command unchanged.
        self._catalog = ToolCatalog(deferred=deferred_tools, get_revealed=get_revealed)
        self._mcp_lifecycle = McpLifecycle()
        self._get_bg_pool = get_bg_pool
        # The event spine (``common.events.EventBus``) is always present: every
        # tool call emits PreToolUse / PostToolUse / FileMutated around it, and
        # control subscribers (the hook layer, the permission gate) fold their
        # influence via the returned outcome. A caller that constructs an
        # executor standalone gets a private bus with no subscribers, which is a
        # no-op fold — so there is a single, unconditional emit path (no
        # bus-present branches to keep in sync).
        self._bus = bus or EventBus()
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

        # Permission engine. Built ONLY when a Role opts in with a
        # PermissionConfig — otherwise None and run_command runs with no
        # approval layer.
        self._permission_engine: PermissionEngine | None = None
        if permission_config is not None:
            ask_user = None
            get_cwd = None
            if role is not None:
                # The interactive approval channel + cwd accessor are published
                # via the Role's capability allowlist (never via getattr).
                caps = role.tool_capabilities()
                ask_user = caps.get("request_approval")
                get_cwd = caps.get("get_cwd")
            # The sandbox boundary (axis B) is orthogonal to the approval mode.
            # Built only when a SandboxConfig is supplied — otherwise no
            # filesystem boundary is enforced.
            sandbox = None
            if permission_config.sandbox is not None:
                sandbox = SandboxGuard(permission_config.sandbox, get_cwd=get_cwd)
            self._permission_engine = PermissionEngine(
                mode=permission_config.mode,
                store=RuleStore.from_config(permission_config),
                ask_user=ask_user,
                sandbox=sandbox,
            )
            # Put the gate ON the control plane, after the hook subscriber, so it
            # evaluates hook-rewritten args and fails closed. The engine stays
            # tool-free — it reads only the PermissionFacts the PreToolUse event
            # carries (resolved by the executor, which owns the tool).
            self._bus.subscribe(PermissionSubscriber(self._permission_engine))

        loop_guard = loop_guard_config or LoopGuardConfig()
        if loop_guard.enabled:
            self._bus.subscribe(
                LoopGuardSubscriber(
                    ThrashDetector(
                        failure_threshold=loop_guard.failure_threshold,
                        no_progress_threshold=loop_guard.no_progress_threshold,
                    ),
                    resolve_readonly=self._is_readonly_tool,
                    sig_of=_call_arg_signature,
                )
            )
        self._settlement = ToolSettlement(
            session_id=self._session_id,
            bus=self._bus,
            get_tool=self._get_tool,
            ledger=self._ledger,
            limit_config=self._limit_config,
            workspace_store=self._workspace_store,
        )
        self._lifecycle = ToolLifecycle(
            session_id=self._session_id,
            declared_tools=tuple(tools or ()),
            role=role,
            pipelines_enabled=pipelines_enabled,
            catalog=self._catalog,
            mcp_lifecycle=self._mcp_lifecycle,
            settlement=self._settlement,
        )
        self._pipeline = ToolExecutionPipeline(
            get_tool=self._get_tool,
            available_names=self._catalog.names,
            bus=self._bus,
            ledger=self._ledger,
            recovery_runner=self._recovery_runner,
            get_bg_pool=self._get_bg_pool,
            settlement=self._settlement,
        )

    def prepare(self) -> None:
        self._lifecycle.prepare()

    def register_tool_instance(self, tool: Any, names: list[str]) -> None:
        """Register an already-constructed BaseTool instance under given names.

        Used for runtime-discovered tools (MCP adapters) that aren't in the
        global registry. They share the same _tools map and dispatch path as
        static tools.

        Args:
            tool: A BaseTool instance.
            names: All names (primary + aliases) that route to this instance.
        """
        self._lifecycle.register(tool, names)

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
        return await self._pipeline.run(name, kwargs or {}, result_id)

    def will_ledger(self, name: str, result_id: str | None) -> bool:
        tool = self._get_tool(name)
        return (
            tool is not None
            and self._ledger is not None
            and result_id is not None
            and tool.resolve_effect() is ToolEffect.EXTERNAL
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
