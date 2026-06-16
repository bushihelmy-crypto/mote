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

import inspect
import uuid
from typing import Any, Callable, Mapping

from metagpt.common.events import FileMutatedEvent, PostToolUseEvent, PreToolUseEvent
from metagpt.common.exception import RecoveryAction, RecoveryRunner, RecoveryStrategy, ToolValidationError
from metagpt.common.schema import DEFAULT_MAX_RESULT_SIZE_CHARS, PermissionConfig, ToolResultLimitConfig
from metagpt.executor import tool_result_limit
from metagpt.executor.base_executor import BaseToolExecutor
from metagpt.executor.permission import PermissionEngine, RuleStore
from metagpt.executor.permission.sandbox import SandboxGuard
from metagpt.executor.tool_result import ToolError, ToolResult
from metagpt.executor.tool_registry import registry as tool_registry
from metagpt.common.logs import log_class
from metagpt.common.observability.langfuse_integration import maybe_span
from metagpt.executor.mcp.universal import UniversalMCP
from metagpt.executor.tasks.types import BgTaskResult
from metagpt.executor.mcp_adapter import MCPToolAdapter
from metagpt.executor.tool_spec_adapter import to_native_tool_specs

# Signature params that are framework plumbing, never LLM-facing arguments.
# (``*args``/``**kwargs`` are detected by parameter *kind*, not by name.)
_NON_ARG_PARAMS = frozenset({"self", "cls"})


def _validate_call_args(call_fn: Callable, tool_name: str, args: dict[str, Any]) -> None:
    """Pre-flight LLM-supplied args against a tool's ``call()`` signature.

    Raises :class:`ToolValidationError` when a required parameter is missing or
    an unexpected one is supplied, so a malformed call surfaces as a structured
    tool-result failure with a clear "which argument" message instead of an
    opaque ``TypeError`` from ``tool.call(**args)``.

    Skipped entirely when ``call()`` accepts ``**kwargs`` (dynamic / MCP tools,
    whose parameters are not statically known) — this is the natural gate that
    limits validation to tools with a statically-declared signature.

    Type checking is intentionally NOT performed: the legacy XML command
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
        parts.append(f"missing required argument(s): {', '.join(missing)}")
    if unexpected:
        parts.append(f"unexpected argument(s): {', '.join(unexpected)}")
    raise ToolValidationError(f"{tool_name}: {'; '.join(parts)}")


# ---------------------------------------------------------------------------
# ToolExecutor — dispatch engine
# ---------------------------------------------------------------------------


@log_class(
    level="DEBUG",
    # Schema introspection getters are pure/derived and called frequently when
    # building prompts — tracing them only adds noise.
    exclude={"get_tool_schemas", "get_mcp_tool_schemas", "get_all_tool_schemas", "get_native_tool_specs"},
)
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
        permission_config: PermissionConfig | None = None,
        bus=None,
        recovery_strategies: Mapping[RecoveryAction, RecoveryStrategy] | None = None,
    ) -> None:
        self._session_id = session_id
        self._mcp: UniversalMCP | None = None
        self._tools: dict[str, Any] = {}  # name -> BaseTool instance (static + dynamic)
        # Optional event bus (``common.events.EventBus``). When set, PreToolUse /
        # PostToolUse events are emitted around the tool call; a subscriber (the
        # hook layer) may deny / mutate / inject context via the folded outcome.
        # None => no events emitted (standalone/test use), exactly like the
        # permission engine opt-in.
        self._bus = bus
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
        # default config reproduces CC's out-of-the-box behavior.
        self._limit_config = limit_config or ToolResultLimitConfig()

        # Permission engine. Built ONLY when a Role opts in with a
        # PermissionConfig — otherwise None and run_command behaves exactly as
        # before (no approval layer), preserving backward compatibility.
        self._permission_engine: PermissionEngine | None = None
        if permission_config is not None:
            ask_human = None
            get_cwd = None
            if role is not None:
                # The interactive approval channel + cwd accessor are published
                # via the Role's capability allowlist (never via getattr).
                caps = role.tool_capabilities()
                ask_human = caps.get("request_approval")
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
                ask_human=ask_human,
                sandbox=sandbox,
            )

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

        args = kwargs or {}

        with maybe_span(f"tool:{name}", **(kwargs or {})):
            # PreToolUse event: emitted before the permission gate. A subscriber
            # (the hook layer) may rewrite the args (updated_args) or block the
            # call outright (deny). Hook deny composes with the permission engine
            # via deny-wins: a hook block returns immediately; a hook allow never
            # overrides an engine deny (the engine still runs below).
            if self._bus is not None:
                outcome = await self._bus.emit(
                    PreToolUseEvent(tool_name=name, tool_input=args, tool_use_id=result_id)
                )
                if outcome.updated_args is not None:
                    args = outcome.updated_args
                if outcome.behavior == "deny" or outcome.stop:
                    reason = outcome.system_message or outcome.stop_reason or "blocked by PreToolUse hook"
                    return ToolResult(output=f"[PERMISSION DENIED] {reason}", success=False)

            # Permission gate: when enabled, evaluate the call before executing. A
            # denied call never reaches tool.call(); an approver may also narrow the
            # arguments via updated_args.
            if self._permission_engine is not None:
                # Most tools touch a single target; a few (ApplyPatch) act on
                # several paths in one call. Evaluate them together via
                # check_multi so a multi-path patch yields one consolidated
                # approval; single-target tools keep the existing check() path.
                targets = tool.permission_targets(args)
                mutates_fs = getattr(tool, "mutates_filesystem", False)
                tool_check = tool.check_permissions(args)
                if len(targets) > 1:
                    decision = await self._permission_engine.check_multi(
                        name,
                        targets=targets,
                        tool_check=tool_check,
                        mutates_fs=mutates_fs,
                    )
                else:
                    decision = await self._permission_engine.check(
                        name,
                        target=targets[0] if targets else "",
                        tool_check=tool_check,
                        mutates_fs=mutates_fs,
                    )
                if decision.behavior == "deny":
                    return ToolResult(output=f"[PERMISSION DENIED] {decision.message}", success=False)
                if decision.updated_args is not None:
                    args = decision.updated_args

            async def _call():
                # Validate inside the recovery loop so a strategy that repairs
                # args is re-checked on retry. A bad-args ToolValidationError is
                # non-retryable (recovery=ABORT): the runner re-raises on the
                # first attempt and the except-ToolError arm below turns it into
                # a failed ToolResult — no extra branch needed here.
                _validate_call_args(tool.call, name, args)
                return await tool.call(**args)

            try:
                # Run under the recovery loop. With an empty registry this is a
                # plain ``await tool.call(**args)`` — typed errors re-raise and
                # are handled by the except arms below exactly as before.
                raw = await self._recovery_runner.run(_call)
            except ToolError as e:
                # Expected, recoverable failure the tool signalled deliberately.
                # Not logged as an error: it is normal control flow (bad args,
                # missing file, etc.), surfaced to the model as a failed tool result.
                return ToolResult(output=str(e), success=False)
            except Exception as e:
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

            # PostToolUse event: emitted after the tool ran (and was normalized).
            # A subscriber (the hook layer) may append extra context to the
            # output or block (mark the result failed with a reason) for the
            # model to react to.
            if self._bus is not None:
                outcome = await self._bus.emit(
                    PostToolUseEvent(
                        tool_name=name,
                        tool_input=args,
                        tool_response=result.output,
                        tool_use_id=result_id,
                    )
                )
                if outcome.additional_context:
                    extra = "\n".join(outcome.additional_context)
                    result.output = f"{result.output}\n{extra}" if result.output else extra
                if outcome.is_blocking:
                    reason = outcome.system_message or outcome.stop_reason or "blocked by PostToolUse hook"
                    result.success = False
                    result.output = f"{result.output}\n[PostToolUse] {reason}" if result.output else f"[PostToolUse] {reason}"

            # After-edit notification: a successful filesystem-mutating tool
            # emits a FileMutatedEvent carrying the written path, so any
            # subscriber can react — the LSP service syncs the doc + collects
            # diagnostics, the file-watcher suppresses echoing our own edit back
            # as an external change. Observation only; best-effort.
            if result.success and getattr(tool, "mutates_filesystem", False) and self._bus is not None:
                path = tool.permission_target(args)
                if path:
                    try:
                        await self._bus.emit(FileMutatedEvent(path=path, tool=name))
                    except Exception:  # noqa: BLE001 — never break the tool call
                        pass

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
