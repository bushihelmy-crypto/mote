"""Ordered post-execution settlement for tool calls."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from typing import Any

from mote.common.events import EventBus, FileMutatedEvent, PostToolUseEvent
from mote.common.events.scope import current_scope
from mote.common.logs import logger
from mote.common.schema import DEFAULT_MAX_RESULT_SIZE_CHARS, ToolResultLimitConfig
from mote.common.workspace import WorkspaceStore
from mote.executor import tool_result_limit
from mote.executor.compress.tool_output import compress_tool_result
from mote.executor.effect_ledger import EffectLedger
from mote.executor.tool_result import ToolResult


class ToolSettlement:
    """Close tool lifecycles in one fixed post-body order."""

    def __init__(
        self,
        *,
        session_id: str,
        bus: EventBus,
        get_tool: Callable[[str], Any],
        ledger: EffectLedger | None,
        limit_config: ToolResultLimitConfig,
        workspace_store: WorkspaceStore,
    ) -> None:
        self._session_id = session_id
        self._bus = bus
        self._get_tool = get_tool
        self._ledger = ledger
        self._limit_config = limit_config
        self._workspace_store = workspace_store

    async def observe(self, event, *, context: str) -> None:
        try:
            await self._bus.observe(event)
        except Exception as exc:  # observation must never mask the operation
            logger.debug(f"ToolSettlement: {context}: {exc}")

    def _post_event(
        self,
        name: str,
        args: dict[str, Any],
        result: ToolResult,
        result_id: str | None,
    ) -> PostToolUseEvent:
        return PostToolUseEvent(
            tool_name=name,
            tool_input=args,
            tool_response=result.output,
            success=result.success,
            error=result.error,
            media=result.media,
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
        await self.observe(
            self._post_event(name, args, result, result_id),
            context=f"not-ran notice for {name} not delivered",
        )
        return result

    async def finish(
        self,
        name: str,
        args: dict[str, Any],
        result: ToolResult,
        result_id: str | None,
        ledgered: bool,
    ) -> ToolResult:
        result = await self._settle(name, args, result, result_id)
        if ledgered and result_id is not None and self._ledger is not None:
            if result.success:
                self._ledger.mark_completed(result_id, name, result=result.output)
            else:
                self._ledger.mark_failed(result_id, name, result=result.output)
        return result

    async def _settle(
        self,
        name: str,
        args: dict[str, Any],
        result: ToolResult,
        result_id: str | None,
    ) -> ToolResult:
        outcome = await self._bus.emit(self._post_event(name, args, result, result_id))
        result = self._apply_post_outcome(result, outcome)
        tool = self._get_tool(name)
        if result.success and tool is not None and getattr(tool, "mutates_filesystem", False):
            path = tool.permission_target(args)
            if path:
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
        return self._limit_result(result, name, result_id)

    @staticmethod
    def _apply_post_outcome(result: ToolResult, outcome) -> ToolResult:
        if outcome is None:
            return result
        if outcome.updated_response is not None:
            result.output = outcome.updated_response
        if outcome.additional_context:
            extra = "\n".join(outcome.additional_context)
            result.output = f"{result.output}\n{extra}" if result.output else extra
        if outcome.is_blocking:
            reason = outcome.system_message or outcome.stop_reason or "blocked by PostToolUse hook"
            result.success = False
            result.output = f"{result.output}\n[PostToolUse] {reason}" if result.output else f"[PostToolUse] {reason}"
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
