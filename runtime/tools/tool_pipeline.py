"""Closed, ordered execution pipeline for one tool call.

The stage order is framework policy, not a plugin surface.  New behavior must
belong to one of these stages so authorization, durability, execution and
settlement cannot be reordered by registration order.
"""

from __future__ import annotations

import itertools
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from mote.contracts.authorization import PermissionFacts
from mote.contracts.foundation.errors.report import ErrorReport, render_error_block
from mote.contracts.ports.tool.deferred import DeferredResultProjector
from mote.contracts.ports.tool.policy import ToolCallPolicy, ToolPermissionFactsProvider
from mote.contracts.tool.arguments import ToolArguments, freeze_tool_arguments
from mote.contracts.tool.effects import ToolEffect
from mote.contracts.tool.errors import ToolNotFoundError, ToolPermissionDeniedError, ToolValidationError
from mote.contracts.tool.execution import ToolExecutionKind
from mote.contracts.tool.identity import ToolInvocationIdentity
from mote.contracts.tool.policy import ToolCallIntent
from mote.kernel.telemetry.events import span
from mote.runtime.events.scope import current_scope
from mote.runtime.resilience.recovery import RecoveryRunner
from mote.runtime.tools.effect_store import ToolEffectState, ToolEffectStore
from mote.runtime.tools.execution_context import AuthorizedToolInvocation, bind_authorized_invocation, bind_tool_call_id
from mote.runtime.tools.tool_binding import ExecutableToolBinding
from mote.runtime.tools.tool_result import ToolResult
from mote.runtime.tools.tool_result_receipt import decode_tool_result_receipt
from mote.runtime.tools.tool_settlement import ToolSettlement

_UNKNOWN_AFTER_CRASH = (
    "<unknown-after-crash>\n"
    "Tool '{name}' (call {call_id}) was started before a restart but its outcome "
    "was never recorded, so re-running it could duplicate an external side effect. "
    "It was NOT re-run. Verify whether the effect already took hold; reissue the "
    "call only if it is safe to retry.\n</unknown-after-crash>"
)


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
    args: ToolArguments
    identity: ToolInvocationIdentity
    tool: ExecutableToolBinding | None = None
    ledgered: bool = False
    authorization_generation: int | None = None


@dataclass(frozen=True)
class LedgerReplay:
    result: ToolResult


class ResolveStage:
    def __init__(
        self,
        get_tool: Callable[[str], ExecutableToolBinding | None],
        available_names: Callable[[], list[str]],
    ) -> None:
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
        self._generations = itertools.count(1)

    async def run(self, execution: ToolExecution) -> ToolResult | None:
        tool = execution.tool
        if tool is None:
            raise RuntimeError("authorization reached without a compiled Tool binding")

        def resolve_facts(args: ToolArguments) -> PermissionFacts:
            if isinstance(tool, ToolPermissionFactsProvider):
                return tool.permission_facts(args, execution.identity)
            mutable_args = dict(args)
            return PermissionFacts(
                targets=tool.permission_targets(mutable_args),
                mutates_fs=tool.mutates_filesystem_for(mutable_args),
                tool_check=tool.check_permissions(mutable_args),
                segments=tool.permission_segments(mutable_args),
            )

        decision = await self._policy.authorize(
            ToolCallIntent(
                identity=execution.identity,
                tool_name=execution.name,
                arguments=execution.args,
                scope=current_scope(),
            ),
            resolve_facts,
        )
        execution.args = freeze_tool_arguments(decision.arguments)
        execution.identity = decision.identity
        if not decision.allowed and isinstance(tool, ToolPermissionFactsProvider):
            tool.release_permission_facts(execution.identity)
        if decision.allowed:
            if isinstance(tool, ToolPermissionFactsProvider):
                try:
                    resolve_facts(execution.args)
                except Exception as exc:
                    tool.release_permission_facts(execution.identity)
                    return failed_result(
                        ToolPermissionDeniedError(f"tool target reservation failed before execution: {exc}")
                    )
            execution.authorization_generation = next(self._generations)
            return None
        reason = decision.reason or "blocked before tool use"
        return failed_result(
            ToolPermissionDeniedError(reason),
            terminate=decision.terminate,
        )


class LedgerStage:
    def __init__(self, store: ToolEffectStore | None) -> None:
        self._store = store

    def run(self, execution: ToolExecution) -> LedgerReplay | ToolResult | None:
        tool = execution.tool
        if tool is None:
            return failed_result(RuntimeError("ledger reached without a compiled Tool binding"))
        effect = tool.resolve_effect_for(dict(execution.args))
        execution.ledgered = self._store is not None and effect is not ToolEffect.PURE
        if not execution.ledgered:
            return None
        store = self._store
        if store is None:
            raise RuntimeError("Tool effect store disappeared after admission")
        call_id = str(execution.identity.invocation_id)
        prior = store.lookup(call_id)
        if prior is not None:
            durable_identity = prior.identity
            if self._binding(durable_identity) != self._binding(execution.identity):
                return failed_result(
                    ToolPermissionDeniedError(
                        f"tool invocation '{call_id}' does not match its durable definition, "
                        "catalog, arguments, owner, or run identity"
                    )
                )
            # A terminal replay is the original attempt's immutable settlement,
            # not a newly executed attempt. Downstream facts project that exact
            # durable attempt identity.
            execution.identity = durable_identity
            if prior.state in {ToolEffectState.SUCCEEDED, ToolEffectState.FAILED}:
                return LedgerReplay(
                    decode_tool_result_receipt(
                        prior.receipt,
                        success=prior.state is ToolEffectState.SUCCEEDED,
                    )
                )
            if prior.capability is ToolEffect.EXTERNAL and not tool.can_resume_started_call(call_id):
                return failed_result(
                    ToolPermissionDeniedError(_UNKNOWN_AFTER_CRASH.format(name=execution.name, call_id=call_id))
                )
        if prior is None:
            store.commit_intent(execution.identity, execution.name, effect)
        return None

    @staticmethod
    def _binding(identity: ToolInvocationIdentity) -> tuple[str, int, str, str, str]:
        return (
            identity.definition_identity,
            identity.catalog_generation,
            identity.arguments_digest,
            identity.owner_id,
            identity.run_id,
        )


class InvokeStage:
    def __init__(
        self,
        recovery_runner: RecoveryRunner,
        deferred_projector: DeferredResultProjector | None,
    ) -> None:
        self._recovery_runner = recovery_runner
        self._deferred_projector = deferred_projector

    async def run(self, execution: ToolExecution) -> ToolResult:
        tool = execution.tool
        if tool is None:
            return failed_result(RuntimeError("invocation reached without a compiled Tool binding"))

        async def call():
            if execution.authorization_generation is None:
                raise RuntimeError("tool invocation reached execution without authorization")
            invocation = AuthorizedToolInvocation(
                identity=execution.identity,
                tool_name=execution.name,
                arguments=execution.args,
                generation=execution.authorization_generation,
            )
            with bind_tool_call_id(str(execution.identity.invocation_id)), bind_authorized_invocation(invocation):
                return await tool.call(**dict(execution.args))

        try:
            raw = await self._recovery_runner.run(call)
        except Exception as exc:  # normalized at the executor boundary
            return failed_result(exc)
        projector = self._deferred_projector
        execution_kind = tool.definition.execution_kind
        if projector is None:
            if execution_kind is ToolExecutionKind.WORKFLOW_DEFERRED:
                return failed_result(
                    RuntimeError("deferred result projector is not installed at the Product composition root")
                )
            return ToolResult.from_tool_return(raw)
        deferred_kind = projector.classify(raw)
        if deferred_kind is not None:
            if execution_kind is ToolExecutionKind.ATOMIC:
                return failed_result(TypeError(f"atomic tool '{execution.name}' returned deferred work"))
            settlement = projector.settle(raw, tool_name=execution.name)
            return ToolResult(
                output=settlement.output,
                success=True,
                execution_value=settlement.execution_value,
                async_work_submission=settlement.submission,
            )
        return ToolResult.from_tool_return(raw)


class ToolExecutionPipeline:
    """Fixed lifecycle: resolve → authorize → ledger → start → invoke → settle."""

    def __init__(
        self,
        *,
        get_tool: Callable[[str], ExecutableToolBinding | None],
        available_names: Callable[[], list[str]],
        policy: ToolCallPolicy,
        effect_store: ToolEffectStore | None,
        recovery_runner: RecoveryRunner,
        deferred_projector: DeferredResultProjector | None,
        settlement: ToolSettlement,
    ) -> None:
        self._resolve = ResolveStage(get_tool, available_names)
        self._authorize = AuthorizeStage(policy)
        self._ledger = LedgerStage(effect_store)
        self._invoke = InvokeStage(recovery_runner, deferred_projector)
        self._settlement = settlement

    async def run(
        self,
        name: str,
        args: ToolArguments,
        identity: ToolInvocationIdentity,
        *,
        binding: ExecutableToolBinding | None = None,
    ) -> ToolResult:
        execution = ToolExecution(name=name, args=freeze_tool_arguments(args), identity=identity, tool=binding)
        if binding is None:
            rejected = self._resolve.run(execution)
            if rejected is not None:
                return await self._settlement.reject(name, dict(args), rejected, identity)
        async with span(f"tool:{execution.name}", attributes=dict(execution.args)):
            rejected = await self._authorize.run(execution)
            if rejected is not None:
                return await self._settlement.reject(execution.name, dict(execution.args), rejected, execution.identity)
            provider = execution.tool if isinstance(execution.tool, ToolPermissionFactsProvider) else None
            try:
                short_circuit = self._ledger.run(execution)
                if isinstance(short_circuit, LedgerReplay):
                    return short_circuit.result
                if short_circuit is not None:
                    return await self._settlement.reject(
                        execution.name,
                        dict(execution.args),
                        short_circuit,
                        execution.identity,
                    )
                await self._settlement.start(
                    execution.name,
                    dict(execution.args),
                    execution.identity,
                )
                result = await self._invoke.run(execution)
                return await self._settlement.finish(
                    execution.name,
                    dict(execution.args),
                    result,
                    execution.identity,
                    execution.ledgered,
                )
            finally:
                if provider is not None:
                    provider.release_permission_facts(execution.identity)
