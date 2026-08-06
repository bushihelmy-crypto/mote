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
from mote.contracts.interaction.approval_identity import ApprovalRequestId
from mote.contracts.ports.tool.approval import ToolApprovalCoordinator, ToolApprovalIntent
from mote.contracts.ports.tool.deferred import DeferredResultProjector
from mote.contracts.ports.tool.policy import ToolCallPolicy, ToolPermissionFactsProvider
from mote.contracts.tool.arguments import ToolArguments, freeze_tool_arguments
from mote.contracts.tool.errors import ToolNotFoundError, ToolPermissionDeniedError, ToolValidationError
from mote.contracts.tool.execution import ToolExecutionKind
from mote.contracts.tool.identity import ToolInvocationIdentity
from mote.contracts.tool.policy import ToolCallIntent
from mote.kernel.telemetry.events import span
from mote.runtime.events.scope import current_scope
from mote.runtime.resilience.recovery import RecoveryRunner
from mote.runtime.tools.execution_context import (
    AuthorizedToolInvocation,
    bind_authorized_invocation,
    bind_fileops_transaction_id,
    bind_tool_call_id,
)
from mote.runtime.tools.tool_binding import ExecutableToolBinding
from mote.runtime.tools.tool_result import ToolResult
from mote.runtime.tools.tool_settlement import ToolSettlement


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
    authorization_generation: int | None = None
    approval_request_id: ApprovalRequestId | None = None
    fileops_transaction_id: str | None = None


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
    def __init__(self, policy: ToolCallPolicy, approval: ToolApprovalCoordinator | None) -> None:
        self._policy = policy
        self._approval = approval
        self._generations = itertools.count(1)

    def bind_approval_coordinator(self, coordinator: ToolApprovalCoordinator | None) -> None:
        self._approval = coordinator

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
        while decision.approval_required:
            coordinator = self._approval
            if coordinator is None:
                return failed_result(
                    ToolPermissionDeniedError("tool approval is required but no durable coordinator is installed")
                )
            resolution = await coordinator.resolve(
                ToolApprovalIntent(
                    execution.identity,
                    execution.name,
                    execution.args,
                    decision.permission_targets,
                    decision.mutates_fs,
                    decision.reason,
                )
            )
            execution.approval_request_id = resolution.request_id
            if resolution.revised_arguments is not None:
                execution.args = freeze_tool_arguments(resolution.revised_arguments)
                execution.identity = execution.identity.with_arguments(execution.args)
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
                continue
            if not resolution.approved:
                return failed_result(
                    ToolPermissionDeniedError("the user rejected this tool call"),
                    terminate=True,
                )
            decision = ToolCallDecision.allow(execution.identity, execution.args, trace=decision.trace)
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
            with (
                bind_tool_call_id(str(execution.identity.invocation_id)),
                bind_authorized_invocation(invocation),
                bind_fileops_transaction_id(execution.fileops_transaction_id),
            ):
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
    """Fixed lifecycle: resolve → authorize → start → invoke → settle."""

    def __init__(
        self,
        *,
        get_tool: Callable[[str], ExecutableToolBinding | None],
        available_names: Callable[[], list[str]],
        policy: ToolCallPolicy,
        approval: ToolApprovalCoordinator | None,
        recovery_runner: RecoveryRunner,
        deferred_projector: DeferredResultProjector | None,
        settlement: ToolSettlement,
    ) -> None:
        self._resolve = ResolveStage(get_tool, available_names)
        self._authorize = AuthorizeStage(policy, approval)
        self._invoke = InvokeStage(recovery_runner, deferred_projector)
        self._settlement = settlement

    def bind_approval_coordinator(self, coordinator: ToolApprovalCoordinator | None) -> None:
        self._authorize.bind_approval_coordinator(coordinator)

    async def run(
        self,
        name: str,
        args: ToolArguments,
        identity: ToolInvocationIdentity,
        *,
        binding: ExecutableToolBinding | None = None,
    ) -> ToolResult:
        execution = ToolExecution(name=name, args=freeze_tool_arguments(args), identity=identity, tool=binding)
        rejected = await self.authorize(execution)
        if rejected is not None:
            return rejected
        return await self.invoke(execution)

    def execution(
        self,
        name: str,
        args: ToolArguments,
        identity: ToolInvocationIdentity,
        *,
        binding: ExecutableToolBinding,
    ) -> ToolExecution:
        return ToolExecution(
            name=name,
            args=freeze_tool_arguments(args),
            identity=identity,
            tool=binding,
        )

    async def authorize(self, execution: ToolExecution) -> ToolResult | None:
        if execution.tool is None:
            rejected = self._resolve.run(execution)
            if rejected is not None:
                return await self._settlement.reject(
                    execution.name,
                    dict(execution.args),
                    rejected,
                    execution.identity,
                )
        async with span(f"tool:{execution.name}", attributes=dict(execution.args)):
            rejected = await self._authorize.run(execution)
            if rejected is not None:
                return await self._settlement.reject(execution.name, dict(execution.args), rejected, execution.identity)
        return None

    async def invoke(self, execution: ToolExecution) -> ToolResult:
        async with span(f"tool:{execution.name}", attributes=dict(execution.args)):
            provider = execution.tool if isinstance(execution.tool, ToolPermissionFactsProvider) else None
            try:
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
                )
            finally:
                if provider is not None:
                    provider.release_permission_facts(execution.identity)
