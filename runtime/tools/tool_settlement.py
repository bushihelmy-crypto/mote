"""Ordered post-execution settlement for tool calls."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import replace
from typing import Any, Literal

from mote.contracts.config.tool import DEFAULT_MAX_RESULT_SIZE_CHARS, ToolResultLimitConfig
from mote.contracts.events.envelope import freeze_json
from mote.contracts.events.file.observation import FileMutatedEvent
from mote.contracts.events.tool import ToolCallFinishedEvent, ToolInvocationStartedEvent, ToolsChangedEvent
from mote.contracts.ports.events.telemetry import TelemetryEmitter
from mote.contracts.ports.tool.policy import ToolResultPolicy
from mote.contracts.tool.effects import ToolEffect
from mote.contracts.tool.identity import ToolInvocationIdentity
from mote.contracts.tool.policy import ToolResultIntent
from mote.runtime.events.scope import current_scope
from mote.runtime.resources import spill as tool_result_limit
from mote.runtime.session.workspace import SessionWorkspace
from mote.runtime.telemetry.logging import logger
from mote.runtime.tools.compress.tool_output import compress_tool_result
from mote.runtime.tools.effect_store import ToolEffectStore
from mote.runtime.tools.tool_binding import ExecutableToolBinding
from mote.runtime.tools.tool_result import ToolResult
from mote.runtime.tools.tool_result_receipt import encode_tool_result_receipt

ToolObservationEvent = FileMutatedEvent | ToolCallFinishedEvent | ToolInvocationStartedEvent | ToolsChangedEvent


class ToolSettlement:
    """Close tool lifecycles in one fixed post-body order."""

    def __init__(
        self,
        *,
        session_id: str,
        telemetry: TelemetryEmitter[ToolObservationEvent],
        get_tool: Callable[[str], ExecutableToolBinding | None],
        effect_store: ToolEffectStore | None,
        limit_config: ToolResultLimitConfig,
        workspace_store: SessionWorkspace,
        policy: ToolResultPolicy,
    ) -> None:
        self._session_id = session_id
        self._telemetry = telemetry
        self._get_tool = get_tool
        self._effect_store = effect_store
        self._limit_config = limit_config
        self._workspace_store = workspace_store
        self._policy = policy

    async def observe(
        self,
        event: ToolObservationEvent,
        *,
        context: str,
    ) -> None:
        try:
            await self._telemetry.emit(event)
        except Exception as exc:  # observation must never mask the operation
            logger.debug(f"ToolSettlement: {context}: {exc}")

    def _finished_event(
        self,
        name: str,
        args: dict[str, Any],
        result: ToolResult,
        identity: ToolInvocationIdentity,
        *,
        outcome: Literal["succeeded", "failed", "rejected"],
    ) -> ToolCallFinishedEvent:
        return ToolCallFinishedEvent(
            tool_name=name,
            identity=identity,
            tool_input=args,
            tool_response=result.output,
            outcome=outcome,
            error=result.error,
            media=list(result.media),
            artifacts=list(result.artifacts),
            file_changes=list(result.file_changes),
            scope=current_scope(),
        )

    async def reject(
        self,
        name: str,
        args: dict[str, Any],
        result: ToolResult,
        identity: ToolInvocationIdentity,
    ) -> ToolResult:
        result = await self._present(
            name,
            args,
            result,
            identity,
            executed=False,
        )
        await self.observe(
            self._finished_event(
                name,
                args,
                result,
                identity,
                outcome="rejected",
            ),
            context=f"not-ran notice for {name} not delivered",
        )
        return result

    async def start(
        self,
        name: str,
        args: dict[str, Any],
        identity: ToolInvocationIdentity,
    ) -> None:
        await self.observe(
            ToolInvocationStartedEvent(
                identity=identity,
                tool_name=name,
                tool_input=args,
                scope=current_scope(),
            ),
            context=f"invocation-start notice for {name} not delivered",
        )

    async def finish(
        self,
        name: str,
        args: dict[str, Any],
        result: ToolResult,
        identity: ToolInvocationIdentity,
        ledgered: bool,
    ) -> ToolResult:
        execution_success = result.success
        result = await self._present(
            name,
            args,
            result,
            identity,
            executed=True,
        )
        await self.observe(
            self._finished_event(
                name,
                args,
                result,
                identity,
                outcome="succeeded" if execution_success else "failed",
            ),
            context=f"finished notice for {name} not delivered",
        )
        tool = self._get_tool(name)
        if execution_success and tool is not None and tool.mutates_filesystem_for(args):
            for path in tool.permission_targets(args):
                await self.observe(
                    FileMutatedEvent(path=path, tool=name),
                    context=f"FileMutatedEvent for {path} not delivered",
                )
        result = compress_tool_result(
            result,
            name,
            args,
            session_id=self._session_id,
            config=self._limit_config,
        )
        result = self._limit_result(result, name, str(identity.invocation_id))
        if ledgered and self._effect_store is not None:
            receipt = encode_tool_result_receipt(result)
            self._effect_store.settle(
                str(identity.invocation_id),
                succeeded=execution_success,
                receipt=receipt,
            )
        return result

    async def _present(
        self,
        name: str,
        args: dict[str, Any],
        result: ToolResult,
        identity: ToolInvocationIdentity,
        *,
        executed: bool,
    ) -> ToolResult:
        tool = self._get_tool(name)
        error = freeze_json(result.error.as_dict(), path="tool result error") if result.error is not None else None
        if error is not None and not isinstance(error, Mapping):
            raise TypeError("tool result error must be a JSON object")
        presentation = await self._policy.present(
            ToolResultIntent(
                identity=identity,
                tool_name=name,
                arguments=args,
                output=result.output,
                execution_success=result.success,
                executed=executed,
                error=error,
                is_readonly=(tool is not None and tool.resolve_effect_for(args) is ToolEffect.PURE),
                scope=current_scope(),
            )
        )
        return replace(
            result,
            output=presentation.output,
            terminate=result.terminate or presentation.terminate,
        )

    def _limit_result(
        self,
        result: ToolResult,
        name: str,
        result_id: str,
    ) -> ToolResult:
        config = self._limit_config
        if not config.enable_tool_result_limit or not result.output or result.media:
            return result
        tool = self._get_tool(name)
        cap = tool.max_result_size_chars if tool is not None else DEFAULT_MAX_RESULT_SIZE_CHARS
        output = tool_result_limit.enforce_tool_result_limit(
            result.output,
            name,
            result_id=result_id,
            session_id=self._session_id,
            max_result_size_chars=cap,
            persist=config.persist_large_tool_results,
            store=self._workspace_store,
        )
        return replace(result, output=output)
