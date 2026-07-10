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

from metagpt.common.events import (
    EventBus,
    FileMutatedEvent,
    PostToolUseEvent,
    PreToolUseEvent,
    ToolsChangedEvent,
    span,
)
from metagpt.common.exception import (
    ErrorReport,
    RecoveryAction,
    RecoveryRunner,
    RecoveryStrategy,
    ToolNotFoundError,
    ToolPermissionDeniedError,
    ToolValidationError,
    render_error_block,
)
from metagpt.common.logs import log_class, logger
from metagpt.common.schema import (
    DEFAULT_MAX_RESULT_SIZE_CHARS,
    PERSISTED_OUTPUT_OPEN_TAG,
    PermissionConfig,
    PermissionFacts,
    ToolResultLimitConfig,
)
from metagpt.executor import tool_result_limit
from metagpt.executor.compress import compress_output
from metagpt.executor.base_executor import BaseToolExecutor
from metagpt.executor.mcp.universal import UniversalMCP
from metagpt.executor.mcp_adapter import MCPToolAdapter
from metagpt.executor.permission import PermissionEngine, PermissionSubscriber, RuleStore
from metagpt.executor.permission.sandbox import SandboxGuard
from metagpt.executor.tasks.bggraph.marker import is_pipeline_tool
from metagpt.executor.tasks.types import BgTaskMode, BgTaskResult
from metagpt.executor.tool_registry import registry as tool_registry
from metagpt.executor.tool_result import ToolError, ToolResult
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
        bus: EventBus | None = None,
        recovery_strategies: Mapping[RecoveryAction, RecoveryStrategy] | None = None,
        get_bg_pool: Callable[[], Any] | None = None,
    ) -> None:
        self._session_id = session_id
        self._mcp: UniversalMCP | None = None
        self._tools: dict[str, Any] = {}  # name -> BaseTool instance (static + dynamic)
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
            # Put the gate ON the control plane, after the hook subscriber, so it
            # evaluates hook-rewritten args and fails closed. The engine stays
            # tool-free — it reads only the PermissionFacts the PreToolUse event
            # carries (resolved by the executor, which owns the tool).
            self._bus.subscribe(PermissionSubscriber(self._permission_engine))

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

    async def deregister_tool(self, name: str) -> bool:
        """Remove a bound tool by any of its names — aliases and resources together.

        The inverse of registration (constructor pre-bind / register_tool_instance).
        Resolves the instance ``name`` routes to, then removes *every* alias key in
        ``_tools`` that routes to that **same instance** (identity, not name — so no
        orphan alias survives), reclaims the instance's per-session resources
        (``cleanup_session`` — the same teardown :meth:`cleanup` runs, but for this
        one tool), and announces the change on the bus (:class:`ToolsChangedEvent`)
        so the volatile views refresh instead of silently drifting: the per-turn
        tool catalog drops the vanished names from its incremental frontier, and the
        compaction pipeline refreshes its reconstructable-tool-name set.

        Returns True when a tool was removed, False when ``name`` is unbound (no-op).
        """
        tool = self._tools.get(name)
        if tool is None:
            return False
        # Every alias routing to the SAME instance goes together (by identity, so
        # aliases pointing at other tools are untouched).
        removed = [n for n, t in self._tools.items() if t is tool]
        for n in removed:
            del self._tools[n]
        # Reclaim per-session resources, mirroring cleanup() for this one instance.
        try:
            tool.cleanup_session(self._session_id)
        except Exception as exc:  # noqa: BLE001 — teardown must not raise
            logger.debug(f"ToolExecutor: cleanup_session for {name} failed: {exc}")
        # Announce so views refresh. Pure observation (no control fold) — the live
        # _tools map is already the source of truth; this only says it changed and
        # carries the post-change facts consumers need (which names went away, the
        # fresh reconstructable set) so no consumer needs a back-ref to the executor.
        try:
            await self._bus.observe(
                ToolsChangedEvent(
                    removed=removed,
                    reconstructable=sorted(self.reconstructable_tool_names()),
                )
            )
        except Exception as exc:  # noqa: BLE001 — a notice never breaks the removal
            logger.debug(f"ToolExecutor: ToolsChangedEvent for {name} not delivered: {exc}")
        return True

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
        args = kwargs or {}

        tool = self._get_tool(name)
        if tool is None:
            available = list(self._tools.keys())
            return await self._reject(
                name,
                args,
                _failed_result(ToolNotFoundError(f"unknown tool '{name}'. Available: {available}")),
                result_id,
            )

        async with span(f"tool:{name}", attributes=args):
            # PreToolUse: the single control-plane chokepoint before execution.
            # Control subscribers run as an ordered reduce — first the hook layer
            # (may rewrite args / block), then the permission gate (evaluates the
            # already-rewritten args and folds allow/deny). The event carries a
            # tool-bound ``resolve_facts`` closure so the gate reads what it needs
            # (targets / mutates_fs / tool_check / segments) without the bus or
            # subscriber layer ever importing a tool. A deny (hook or gate) or a
            # stop halts the call; ``updated_args`` narrows it.
            def _resolve_facts(a: dict) -> PermissionFacts:
                return PermissionFacts(
                    targets=tool.permission_targets(a),
                    mutates_fs=getattr(tool, "mutates_filesystem", False),
                    tool_check=tool.check_permissions(a),
                    segments=tool.permission_segments(a),
                )

            outcome = await self._bus.emit(
                PreToolUseEvent(
                    tool_name=name,
                    tool_input=args,
                    tool_use_id=result_id,
                    resolve_facts=_resolve_facts,
                )
            )
            # ``None`` when no control subscriber maps the event (no hook, no gate).
            if outcome is not None:
                if outcome.updated_args is not None:
                    args = outcome.updated_args
                if outcome.behavior == "deny" or outcome.stop:
                    reason = outcome.system_message or outcome.stop_reason or "blocked before tool use"
                    # ``stop`` marks a terminal block (a real user rejection at the
                    # approval prompt, or a hook veto) — the call fails AND the react
                    # loop ends. A plain ``deny`` (rule/mode/policy/sandbox) only
                    # fails this call; the loop keeps going so the model can replan.
                    return await self._reject(
                        name,
                        args,
                        _failed_result(ToolPermissionDeniedError(reason), terminate=outcome.stop),
                        result_id,
                    )

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
                # plain ``await tool.call(**args)`` — a typed ``ToolError`` (a
                # deliberate, expected failure: bad args, missing file) or an
                # unexpected exception both re-raise here.
                raw = await self._recovery_runner.run(_call)
            except Exception as e:
                # Both cases normalize to the same failed <error> shape via
                # ``_failed_result``: ``ErrorReport.from_exception`` maps the code
                # (a typed ToolError keeps its code; an unexpected exception
                # degrades to UNKNOWN), so every tool failure has one shape and
                # downstream consumers (hooks/telemetry) get structure. The tool
                # *ran*, so the failure ``_settle``s on the control plane, where a
                # PostToolUse hook may still rewrite/annotate/block it.
                return await self._settle(name, args, _failed_result(e), result_id)

            # BgTaskResult: dispatch based on explicit mode.
            if isinstance(raw, BgTaskResult):
                # Submit poll to background pool if present
                task_id = None
                if raw.poll_factory is not None and self._get_bg_pool is not None:
                    pool = self._get_bg_pool()
                    if pool is not None:
                        task_id = pool.submit(
                            raw.poll_factory,
                            command_name=raw.command_name or name,
                            graph_meta=raw.graph_meta,
                            progress=True,
                        )

                if raw.mode == BgTaskMode.FOREGROUND:
                    output = str(raw.result) if raw.result is not None else ""
                elif raw.mode == BgTaskMode.BACKGROUND:
                    task_ref = f" (task_id: {task_id})" if task_id is not None else ""
                    output = (
                        f"Background task '{raw.command_name or name}' submitted{task_ref}. "
                        "Running asynchronously — you will be notified when it completes."
                    )
                else:
                    # HYBRID — immediate result + bg continues
                    output = str(raw.result)
                return await self._settle(
                    name, args, ToolResult(output=output, success=True, data=raw), result_id
                )

            # Normalize the raw return into a ToolResult. A returned ToolResult is
            # used as-is; a plain value is always treated as success — failure is
            # signalled structurally (raise ToolError above, or return
            # ToolResult(success=False)), never by sniffing the output text.
            result = ToolResult.from_tool_return(raw)
            return await self._settle(name, args, result, result_id)

    def _apply_post_outcome(self, result: ToolResult, outcome) -> ToolResult:
        """Fold a PostToolUse :class:`ControlOutcome` into the result.

        A subscriber (the hook layer) may rewrite the output text
        (``updated_response``), append extra context to it, or block (mark the
        result failed with a reason) for the model to react to. ``None`` when no
        control subscriber maps the event (no hook wired) — the result is
        returned untouched.
        """
        if outcome is None:
            return result
        # An output-rewrite (truncate/redact) replaces the base text before any
        # context is appended on top of it.
        if outcome.updated_response is not None:
            result.output = outcome.updated_response
        if outcome.additional_context:
            extra = "\n".join(outcome.additional_context)
            result.output = f"{result.output}\n{extra}" if result.output else extra
        if outcome.is_blocking:
            reason = outcome.system_message or outcome.stop_reason or "blocked by PostToolUse hook"
            result.success = False
            result.output = (
                f"{result.output}\n[PostToolUse] {reason}" if result.output else f"[PostToolUse] {reason}"
            )
        return result

    def _post_event(
        self, name: str, args: dict[str, Any], result: ToolResult, result_id: str | None
    ) -> PostToolUseEvent:
        """Build the one PostToolUse event shape, from the settled result's facts.

        The single place the event is constructed, so every exit — whether the
        tool ran (:meth:`_settle`) or was rejected before running
        (:meth:`_reject`) — ships identical structure (``success``/``error``/
        ``media``/``file_changes``). A new fact added to the event is a one-line
        change here that both planes inherit; the shape can never drift between them.
        """
        return PostToolUseEvent(
            tool_name=name,
            tool_input=args,
            tool_response=result.output,
            success=result.success,
            error=result.error,
            media=result.media_artifacts(),
            file_changes=result.file_changes,
            tool_use_id=result_id,
        )

    async def _reject(
        self, name: str, args: dict[str, Any], result: ToolResult, result_id: str | None
    ) -> ToolResult:
        """Close the lifecycle for a call whose tool **never ran**.

        Used by the pre-execution exits (unknown tool / pre-flight hook or
        permission deny). The PostToolUse event is fanned to **observers only**
        (``observe``): the front-end still gets its one lifecycle-end event (the
        row closes) but no hook control fires (CC-aligned: PostToolUse does not
        fire when PreToolUse blocked the call). The ``terminate`` marker on the
        failed result is preserved. Best-effort — a notice never masks the failure.
        """
        try:
            await self._bus.observe(self._post_event(name, args, result, result_id))
        except Exception as exc:  # noqa: BLE001 — never mask the failure
            logger.debug(f"ToolExecutor: not-ran notice for {name} not delivered: {exc}")
        return result

    async def _settle(
        self, name: str, args: dict[str, Any], result: ToolResult, result_id: str | None
    ) -> ToolResult:
        """Close the lifecycle for a call whose tool body **ran** (success, ``raise``,
        or BgTask).

        The PostToolUse event goes on the **control plane** (``emit``), so a
        PostToolUse hook may rewrite/annotate/block the result (CC-aligned:
        PostToolUse hooks fire on tool errors too). Then the mutated-filesystem
        notice, semantic compression and the size cap run over the settled result.
        """
        outcome = await self._bus.emit(self._post_event(name, args, result, result_id))
        result = self._apply_post_outcome(result, outcome)

        # After-edit notification: a successful filesystem-mutating tool emits a
        # FileMutatedEvent carrying the written path, so any subscriber can react
        # — the LSP service syncs the doc + collects diagnostics, the file-watcher
        # suppresses echoing our own edit back as an external change. Observation
        # only; best-effort.
        tool = self._get_tool(name)
        if result.success and getattr(tool, "mutates_filesystem", False):
            path = tool.permission_target(args)
            if path:
                try:
                    await self._bus.emit(FileMutatedEvent(path=path, tool=name))
                except Exception as exc:  # noqa: BLE001 — never break the tool call
                    logger.debug(f"ToolExecutor: FileMutatedEvent emit for {path} failed: {exc}")

        # Semantic compression runs BEFORE the size cap: it structurally shrinks
        # known verbose output (git/pytest/ruff) while stashing the full original
        # on disk for retrieval. The cap then bounds whatever remains. Both are
        # fail-safe and never touch ``result.success``.
        result = self._compress_result(result, name, args)
        return self._limit_result(result, name, result_id)

    def _compress_result(self, result: ToolResult, name: str, args: dict[str, Any]) -> ToolResult:
        """Structurally compress a shell tool's output when it is understood.

        Applied only for the shell tools (Bash/Terminal, plus a Jupyter
        ``!shell`` magic), where the LLM-issued command is known, and only when
        :func:`compress_output` recognises the command family and produces
        something smaller. On success the *full*
        original output is persisted to disk (a ``-raw-`` id namespace, distinct
        from the size-cap persistence) and a marker line naming that file is
        prepended, so the model can ``Read`` the exact original on demand.

        Fail-safe: media results, empty/already-persisted output, and the
        config-off case are skipped; ``result.success`` is never modified so a
        failed command's exit signal is preserved.
        """
        cfg = self._limit_config
        if not cfg.enable_output_compression or not result.output:
            return result
        # Media goes to the model verbatim; never rewrite it.
        if result.images or result.pdfs:
            return result
        # Already wrapped by the size-cap layer on a prior turn — leave it.
        if result.output.startswith(PERSISTED_OUTPUT_OPEN_TAG):
            return result

        command = self._command_for_compression(name, args)
        if not command:
            return result

        outcome = compress_output(
            command,
            result.output,
            min_chars=cfg.compression_min_output_chars,
            max_input_chars=cfg.compression_max_input_chars,
        )
        if not outcome.applied:
            return result

        # Persist the full original first, so the marker can name its path. The
        # ``raw-`` id namespace keeps it distinct from the size-cap's own file.
        raw_id = f"raw-{uuid.uuid4().hex}"
        full_path = tool_result_limit._persist(result.output, raw_id, self._session_id, None)
        location = f"; full output: {full_path}" if full_path else ""
        marker = f"[compressed: {outcome.label}; saved {outcome.saved_chars} chars{location}]"
        logger.debug(
            f"ToolExecutor: compressed {name} output via {outcome.label} "
            f"({outcome.original_chars} -> {outcome.compressed_chars} chars)"
        )
        result.output = f"{marker}\n{outcome.text}"
        return result

    def _command_for_compression(self, name: str, args: dict[str, Any]) -> str | None:
        """Best-effort command line for routing a shell tool's output.

        Bash carries the exact command in ``args["command"]``. Terminal drives a
        persistent PTY; its ``args["input"]`` may be interactive keystrokes, so
        only the first line is used as a routing hint (``command_prefix`` is
        tolerant, and an unrecognised prefix simply skips compression).

        Jupyter runs *Python code*, not shell commands, so it is only routed for
        an IPython ``!shell`` magic on the first line (``!pytest`` / ``!git
        diff``): the leading ``!`` is stripped and the rest treated as a command.
        A plain-Python first line yields ``None`` — never sniffed as a command,
        so ordinary ``print()`` output is never mistaken for pytest/lint output.

        Any other tool returns ``None`` (not compressed).
        """
        if name == "Bash":
            command = args.get("command")
            return command if isinstance(command, str) else None
        if name == "Terminal":
            value = args.get("input")
            if isinstance(value, str) and value.strip():
                return value.splitlines()[0]
            return None
        if name in ("Jupyter", "Python"):
            code = args.get("code")
            if not isinstance(code, str):
                return None
            first = code.splitlines()[0].strip() if code.splitlines() else ""
            # Only an IPython ``!shell`` magic is a real command; strip the ``!``.
            if first.startswith("!"):
                return first[1:].strip() or None
            return None
        return None

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

    def _is_pipeline_tool(self, tool) -> bool:
        """Return True if the tool is backed by a compiled BgGraph pipeline."""
        return is_pipeline_tool(tool)

    def _category(self, tool) -> str:
        """Classify a tool into one of three categories.

        MCP adapters and pipeline tools are runtime/graph-backed and are listed
        in their own system-prompt sections ("# MCP Tools" / "# Pipeline
        Tools"); everything else is a built-in command. MCP is checked first
        since an MCP adapter never wires a compiled graph.
        """
        if self._is_mcp_tool(tool):
            return "mcp"
        if self._is_pipeline_tool(tool):
            return "pipeline"
        return "builtin"

    def _schemas_for(self, category: str | None) -> dict[str, dict]:
        """Collect deduplicated tool schemas.

        Filters to ``category`` when given; ``None`` returns every category.
        """
        schemas: dict[str, dict] = {}
        seen_ids: set[int] = set()
        for tool in self._tools.values():
            if id(tool) in seen_ids:
                continue
            seen_ids.add(id(tool))
            if category is not None and self._category(tool) != category:
                continue
            schema = tool.tool_schema()
            schemas[schema["name"]] = schema
        return schemas

    def get_tool_schemas(self) -> dict[str, dict]:
        """Return schemas for built-in tools only (excludes MCP and pipeline).

        Returns:
            dict mapping primary tool name -> schema dict.
            Deduplicates aliases so each tool appears once.
        """
        return self._schemas_for("builtin")

    def get_mcp_tool_schemas(self) -> dict[str, dict]:
        """Return schemas for MCP tools only.

        Returns:
            dict mapping namespaced tool name (server:tool) -> schema dict.
            Deduplicates aliases so each tool appears once.
        """
        return self._schemas_for("mcp")

    def get_pipeline_tool_schemas(self) -> dict[str, dict]:
        """Return schemas for pipeline tools only (compiled-graph backed).

        Returns:
            dict mapping primary tool name -> schema dict.
            Deduplicates aliases so each tool appears once.
        """
        return self._schemas_for("pipeline")

    def get_all_tool_schemas(self) -> dict[str, dict]:
        """Return schemas for all declared tools (built-in + MCP + pipeline).

        Returns:
            dict mapping primary tool name -> schema dict.
            Deduplicates aliases so each tool appears once.
        """
        return self._schemas_for(None)

    def reconstructable_tool_names(self) -> frozenset[str]:
        """Names (primary + aliases) of bound tools whose results are re-derivable.

        A tool self-declares this via the ``reconstructable`` ClassVar (see
        :class:`~metagpt.executor.base_tool.BaseTool`). The compaction pipeline
        folds/clears only these tools' result bodies, since the information is
        recoverable (re-read the file, re-run the query). Every name a tool routes
        under is included so the Transcript matches whichever alias the model used.
        """
        names: set[str] = set()
        for name, tool in self._tools.items():
            if getattr(tool, "reconstructable", False):
                names.add(name)
        return frozenset(names)

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

    async def reload_mcp(self, mcps: list[str] | None = None) -> bool:
        """Re-initialize MCP from the current ``mcp_config.json`` (hot-reload).

        The reentrant sibling of :meth:`init_mcp`, driven by the file watcher
        when ``mcp_config.json`` changes (mirrors skill hot-reload). Tears the
        old MCP adapters out of the ``_tools`` map by identity, drops the old
        clients, then re-runs discovery against the freshly read config and
        re-registers whatever is now defined. A single ``ToolsChangedEvent``
        carries the removed names so the volatile views refresh: the per-turn
        tool catalog drops them from its incremental frontier (so a server that
        is still present is re-announced next turn with any new schema), and the
        native channel simply rebuilds ``tool_specs`` on the next request.

        Best-effort and non-throwing. Returns True when a reload ran, False when
        it was a no-op (no ``mcps`` declared for this role).
        """
        if not mcps:
            return False

        # Snapshot the currently-bound MCP adapter names before teardown, so we
        # can announce exactly what went away (identity-deduped like cleanup()).
        removed: list[str] = []
        seen_ids: set[int] = set()
        for name, tool in self._tools.items():
            if self._category(tool) != "mcp":
                continue
            removed.append(name)
            if id(tool) not in seen_ids:
                seen_ids.add(id(tool))
                try:
                    tool.cleanup_session(self._session_id)
                except Exception as exc:  # noqa: BLE001 — teardown must not raise
                    logger.debug(f"ToolExecutor: cleanup_session for {name} failed: {exc}")
        for name in removed:
            del self._tools[name]

        # Drop the old MCP manager (closes its clients) and rebuild from disk.
        if self._mcp is not None:
            await self._mcp.cleanup_clients()
        self._mcp = UniversalMCP()
        await self._mcp.initialize(server_names=mcps)
        self._mcp.register_tools(self)

        # Announce the churn so volatile views refresh (same contract as
        # deregister_tool). Report the removed names — the catalog re-announces
        # any that re-registered; native rebuilds tool_specs regardless.
        try:
            await self._bus.observe(
                ToolsChangedEvent(
                    removed=removed,
                    reconstructable=sorted(self.reconstructable_tool_names()),
                )
            )
        except Exception as exc:  # noqa: BLE001 — a notice never breaks the reload
            logger.debug(f"ToolExecutor: ToolsChangedEvent after MCP reload not delivered: {exc}")
        return True

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
