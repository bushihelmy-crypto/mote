"""Ordered post-execution settlement for tool calls."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from typing import Any, Literal

from mote.contracts.config.tool import DEFAULT_MAX_RESULT_SIZE_CHARS, ToolResultLimitConfig
from mote.contracts.events.file.observation import FileMutatedEvent
from mote.contracts.events.tool import ToolCallFinishedEvent, ToolInvocationStartedEvent
from mote.contracts.ports.tool.policy import ToolResultPolicy
from mote.contracts.tool.effects import ToolEffect
from mote.contracts.tool.policy import ToolResultIntent
from mote.runtime.events.scope import current_scope
from mote.runtime.events.telemetry import TelemetryRuntime
from mote.runtime.ledger import RunJournal
from mote.runtime.resources import spill as tool_result_limit
from mote.runtime.session.workspace import SessionWorkspace
from mote.runtime.telemetry.logging import logger
from mote.runtime.tools.compress.tool_output import compress_tool_result
from mote.runtime.tools.tool_result import ToolResult
from mote.runtime.tools.tool_result_receipt import encode_tool_result_receipt


class ToolSettlement:
    """Close tool lifecycles in one fixed post-body order."""

    def __init__(
        self,
        *,
        session_id: str,
        telemetry: TelemetryRuntime,
        get_tool: Callable[[str], Any],
        journal: RunJournal | None,
        limit_config: ToolResultLimitConfig,
        workspace_store: SessionWorkspace,
        policy: ToolResultPolicy,
    ) -> None:
        self._session_id = session_id
        self._telemetry = telemetry
        self._get_tool = get_tool
        self._journal = journal
        self._limit_config = limit_config
        self._workspace_store = workspace_store
        self._policy = policy

    async def observe(self, event: object, *, context: str) -> None:
        try:
            await self._telemetry.emit(event)
        except Exception as exc:  # observation must never mask the operation
            logger.debug(f"ToolSettlement: {context}: {exc}")

    def _finished_event(
        self,
        name: str,
        args: dict[str, Any],
        result: ToolResult,
        result_id: str | None,
        *,
        outcome: Literal["succeeded", "failed", "rejected"],
    ) -> ToolCallFinishedEvent:
        return ToolCallFinishedEvent(
            tool_name=name,
            tool_input=args,
            tool_response=result.output,
            outcome=outcome,
            error=result.error,
            media=result.media,
            artifacts=result.artifacts,
            file_changes=result.file_changes,
            tool_use_id=result_id,
            scope=current_scope(),
        )

    async def reject(
        self,
        name: str,
        args: dict[str, Any],
        result: ToolResult,
        result_id: str | None,
    ) -> ToolResult:
        result = await self._present(
            name,
            args,
            result,
            result_id,
            executed=False,
        )
        await self.observe(
            self._finished_event(
                name,
                args,
                result,
                result_id,
                outcome="rejected",
            ),
            context=f"not-ran notice for {name} not delivered",
        )
        return result

    async def start(
        self,
        name: str,
        args: dict[str, Any],
        result_id: str | None,
    ) -> None:
        await self.observe(
            ToolInvocationStartedEvent(
                tool_name=name,
                tool_input=args,
                tool_use_id=result_id,
                scope=current_scope(),
            ),
            context=f"invocation-start notice for {name} not delivered",
        )

    async def finish(
        self,
        name: str,
        args: dict[str, Any],
        result: ToolResult,
        result_id: str | None,
        ledgered: bool,
    ) -> ToolResult:
        execution_success = result.success
        result = await self._present(
            name,
            args,
            result,
            result_id,
            executed=True,
        )
        await self.observe(
            self._finished_event(
                name,
                args,
                result,
                result_id,
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
        result = self._limit_result(result, name, result_id)
        if ledgered and result_id is not None and self._journal is not None:
            receipt = encode_tool_result_receipt(result)
            if execution_success:
                self._journal.record_completed(result_id, payload=receipt)
            else:
                self._journal.record_failed(result_id, payload=receipt)
        return result

    async def _present(
        self,
        name: str,
        args: dict[str, Any],
        result: ToolResult,
        result_id: str | None,
        *,
        executed: bool,
    ) -> ToolResult:
        tool = self._get_tool(name)
        error = result.error.as_dict() if result.error is not None else None
        presentation = await self._policy.present(
            ToolResultIntent(
                tool_name=name,
                arguments=args,
                output=result.output,
                execution_success=result.success,
                executed=executed,
                error=error,
                tool_call_id=result_id,
                is_readonly=(tool is not None and tool.resolve_effect_for(args) is ToolEffect.PURE),
                scope=current_scope(),
            )
        )
        result.output = presentation.output
        result.terminate = result.terminate or presentation.terminate
        return result

    def _limit_result(
        self,
        result: ToolResult,
        name: str,
        result_id: str | None,
    ) -> ToolResult:
        config = self._limit_config
        if not config.enable_tool_result_limit or not result.output or result.media:
            return result
        tool = self._get_tool(name)
        cap = getattr(tool, "max_result_size_chars", DEFAULT_MAX_RESULT_SIZE_CHARS)
        result.output = tool_result_limit.enforce_tool_result_limit(
            result.output,
            name,
            result_id=result_id or uuid.uuid4().hex,
            session_id=self._session_id,
            max_result_size_chars=cap,
            persist=config.persist_large_tool_results,
            store=self._workspace_store,
        )
        return result
