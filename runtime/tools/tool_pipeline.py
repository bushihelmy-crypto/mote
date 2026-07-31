"""Closed, ordered execution pipeline for one tool call.

The stage order is framework policy, not a plugin surface.  New behavior must
belong to one of these stages so authorization, durability, execution and
settlement cannot be reordered by registration order.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, cast

from mote.contracts.authorization import PermissionFacts
from mote.contracts.ports.tool.policy import ToolCallPolicy
from mote.contracts.tool.effects import ToolEffect
from mote.contracts.tool.execution import ToolExecutionKind
from mote.contracts.tool.policy import ToolCallIntent
from mote.runtime.errors import (
    ErrorReport,
    RecoveryRunner,
    ToolNotFoundError,
    ToolPermissionDeniedError,
    ToolValidationError,
    render_error_block,
)
from mote.runtime.events import span
from mote.runtime.events.scope import current_scope
from mote.runtime.ledger import COMPLETED, FAILED, KIND_TOOL, RunJournal
from mote.runtime.presentation import plural
from mote.runtime.tools.execution_context import bind_tool_call_id
from mote.runtime.tools.tool_result import ToolResult
from mote.runtime.tools.tool_result_receipt import decode_tool_result_receipt
from mote.runtime.tools.tool_settlement import ToolSettlement

_NON_ARG_PARAMS = frozenset({"self", "cls"})
_UNKNOWN_AFTER_CRASH = (
    "<unknown-after-crash>\n"
    "Tool '{name}' (call {call_id}) was started before a restart but its outcome "
    "was never recorded, so re-running it could duplicate an external side effect. "
    "It was NOT re-run. Verify whether the effect already took hold; reissue the "
    "call only if it is safe to retry.\n</unknown-after-crash>"
)


def validate_call_args(call_fn: Callable, tool_name: str, args: dict[str, Any]) -> None:
    try:
        signature = inspect.signature(call_fn)
    except (TypeError, ValueError):
        return
    known: set[str] = set()
    required: set[str] = set()
    for name, parameter in signature.parameters.items():
        if parameter.kind is inspect.Parameter.VAR_KEYWORD:
            return
        if parameter.kind is inspect.Parameter.VAR_POSITIONAL or name in _NON_ARG_PARAMS:
            continue
        known.add(name)
        if parameter.default is inspect.Parameter.empty:
            required.add(name)
    missing = sorted(required - args.keys())
    unexpected = sorted(set(args) - known)
    if not missing and not unexpected:
        return
    parts = []
    if missing:
        parts.append(f"missing required {plural('argument', len(missing))}: {', '.join(missing)}")
    if unexpected:
        parts.append(f"unexpected {plural('argument', len(unexpected))}: {', '.join(unexpected)}")
    raise ToolValidationError(f"{tool_name}: {'; '.join(parts)}")


def failed_result(exc: Exception, *, terminate: bool = False) -> ToolResult:
    report = ErrorReport.from_exception(exc)
    return ToolResult(
        output=render_error_block(report),
        success=False,
        error=report,
        terminate=terminate,
    )


@dataclass
class ToolExecution:
    name: str
    args: dict[str, Any]
    result_id: str | None
    tool: Any = None
    ledgered: bool = False


@dataclass(frozen=True)
class LedgerReplay:
    result: ToolResult


class ResolveStage:
    def __init__(self, get_tool: Callable[[str], Any], available_names: Callable[[], list[str]]) -> None:
        self._get_tool = get_tool
        self._available_names = available_names

    def run(self, execution: ToolExecution) -> ToolResult | None:
        execution.tool = self._get_tool(execution.name)
        if execution.tool is None:
            return failed_result(
                ToolNotFoundError(f"unknown tool '{execution.name}'. Available: {self._available_names()}")
            )
        execution.name = execution.tool.name or execution.name
        return None


class AuthorizeStage:
    def __init__(self, policy: ToolCallPolicy) -> None:
        self._policy = policy

    async def run(self, execution: ToolExecution) -> ToolResult | None:
        tool = execution.tool

        def resolve_facts(args: dict) -> PermissionFacts:
            return PermissionFacts(
                targets=tool.permission_targets(args),
                mutates_fs=tool.mutates_filesystem_for(args),
                tool_check=tool.check_permissions(args),
                segments=tool.permission_segments(args),
            )

        decision = await self._policy.authorize(
            ToolCallIntent(
                tool_name=execution.name,
                arguments=execution.args,
                tool_call_id=execution.result_id,
                scope=current_scope(),
            ),
            resolve_facts,
        )
        execution.args = dict(decision.arguments)
        if decision.allowed:
            return None
        reason = decision.reason or "blocked before tool use"
        return failed_result(
            ToolPermissionDeniedError(reason),
            terminate=decision.terminate,
        )


class LedgerStage:
    def __init__(self, journal: RunJournal | None) -> None:
        self._journal = journal

    def run(self, execution: ToolExecution) -> LedgerReplay | ToolResult | None:
        effect = execution.tool.resolve_effect_for(execution.args)
        execution.ledgered = (
            self._journal is not None and execution.result_id is not None and effect is not ToolEffect.PURE
        )
        if not execution.ledgered:
            return None
        journal = cast(RunJournal, self._journal)
        call_id = cast(str, execution.result_id)
        prior = journal.replay(call_id)
        if prior is not None:
            if prior.status in {COMPLETED, FAILED}:
                return LedgerReplay(
                    decode_tool_result_receipt(
                        prior.payload,
                        success=prior.success,
                    )
                )
            if prior.effect == ToolEffect.EXTERNAL.value and not execution.tool.can_resume_started_call(call_id):
                return failed_result(
                    ToolPermissionDeniedError(_UNKNOWN_AFTER_CRASH.format(name=execution.name, call_id=call_id))
                )
        if prior is None:
            journal.record_started(
                call_id,
                KIND_TOOL,
                effect.value,
                name=execution.name,
                tool_call_id=call_id,
            )
        return None


class InvokeStage:
    def __init__(self, recovery_runner: RecoveryRunner, get_bg_pool: Callable[[], Any] | None) -> None:
        self._recovery_runner = recovery_runner
        self._get_bg_pool = get_bg_pool

    async def run(self, execution: ToolExecution) -> ToolResult:
        async def call():
            validation_callable = getattr(execution.tool, "validation_callable", execution.tool.call)
            validate_call_args(validation_callable, execution.name, execution.args)
            with bind_tool_call_id(execution.result_id):
                return await execution.tool.call(**execution.args)

        try:
            raw = await self._recovery_runner.run(call)
        except Exception as exc:  # normalized at the executor boundary
            return failed_result(exc)
        if bool(getattr(raw, "is_background_result", False)):
            definition = getattr(execution.tool, "definition", None)
            kind = getattr(
                definition,
                "execution_kind",
                ToolExecutionKind.ATOMIC,
            )
            if kind is ToolExecutionKind.ATOMIC:
                return failed_result(TypeError(f"atomic tool '{execution.name}' returned deferred work"))
            pool = self._get_bg_pool() if self._get_bg_pool is not None else None
            return raw.to_tool_result(pool, execution.name)
        return ToolResult.from_tool_return(raw)


class ToolExecutionPipeline:
    """Fixed lifecycle: resolve → authorize → ledger → start → invoke → settle."""

    def __init__(
        self,
        *,
        get_tool: Callable[[str], Any],
        available_names: Callable[[], list[str]],
        policy: ToolCallPolicy,
        journal: RunJournal | None,
        recovery_runner: RecoveryRunner,
        get_bg_pool: Callable[[], Any] | None,
        settlement: ToolSettlement,
    ) -> None:
        self._resolve = ResolveStage(get_tool, available_names)
        self._authorize = AuthorizeStage(policy)
        self._ledger = LedgerStage(journal)
        self._invoke = InvokeStage(recovery_runner, get_bg_pool)
        self._settlement = settlement

    async def run(
        self,
        name: str,
        args: dict[str, Any],
        result_id: str | None,
    ) -> ToolResult:
        execution = ToolExecution(name=name, args=args, result_id=result_id)
        rejected = self._resolve.run(execution)
        if rejected is not None:
            return await self._settlement.reject(name, args, rejected, result_id)
        async with span(f"tool:{execution.name}", attributes=execution.args):
            rejected = await self._authorize.run(execution)
            if rejected is not None:
                return await self._settlement.reject(execution.name, execution.args, rejected, execution.result_id)
            short_circuit = self._ledger.run(execution)
            if isinstance(short_circuit, LedgerReplay):
                return short_circuit.result
            if short_circuit is not None:
                return await self._settlement.reject(
                    execution.name,
                    execution.args,
                    short_circuit,
                    execution.result_id,
                )
            await self._settlement.start(
                execution.name,
                execution.args,
                execution.result_id,
            )
            result = await self._invoke.run(execution)
            return await self._settlement.finish(
                execution.name,
                execution.args,
                result,
                execution.result_id,
                execution.ledgered,
            )
