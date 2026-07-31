"""Transactional execution of one model-emitted tool batch."""

from __future__ import annotations

from collections.abc import Callable

from mote.contracts.conversation import AIMessage, CauseBy, Message
from mote.contracts.execution.models import MutationResult, MutationStatus
from mote.contracts.model.inference import InferenceResult
from mote.contracts.model.turn import ModelTurn
from mote.contracts.ports.execution.transaction import ExecutionTransactionPort
from mote.contracts.tool.catalog import (
    ToolBindingSnapshot,
    ToolDispatchRequest,
    ToolExecutionOutcome,
    ToolExecutionPort,
)
from mote.kernel.commands.channel import CommandChannel, join_command_outputs
from mote.kernel.commands.contracts import ExecutedCommand
from mote.kernel.execution.context import ExecutionContext
from mote.kernel.execution.operations.actions import ActionDispatcher
from mote.kernel.inference.base import BaseInferenceEngine
from mote.kernel.telemetry.events import span


class ActionExecutionService:
    """Execute and durably record one semantic action batch.

    The service owns the transaction ordering between assistant call recording,
    external effects, result pairing, and think-journal reaping. Graph topology
    only decides when an action batch runs.
    """

    def __init__(
        self,
        *,
        context: Callable[[], ExecutionContext],
        channel: Callable[[], CommandChannel],
        inference_engine: BaseInferenceEngine,
        tool_execution_port: ToolExecutionPort[ToolExecutionOutcome],
        tool_snapshot: Callable[[], ToolBindingSnapshot | None],
        transaction: ExecutionTransactionPort,
        report_inference_result: Callable[[InferenceResult], None],
        set_active: Callable[[bool], None],
        dispatcher: ActionDispatcher | None = None,
    ) -> None:
        self._context = context
        self._channel = channel
        self._inference_engine = inference_engine
        self._tool_execution_port = tool_execution_port
        self._tool_snapshot = tool_snapshot
        self._transaction = transaction
        self._report_think_result = report_inference_result
        self._set_active = set_active
        self._dispatcher = dispatcher or ActionDispatcher()

    async def execute(self, turn: ModelTurn | None = None) -> Message:
        async with span("act"):
            channel = self._channel()
            if turn is None:
                turn = await channel.model_turn(self._inference_engine.result)
            commands = self._dispatcher.tool_commands(turn, set(self._context().tools))
            self._report_think_result(self._inference_engine.result)
            content = self._inference_engine.result.content
            operation_prefix = self._inference_engine.model_call_id or "inference"

            executed = [
                ExecutedCommand(
                    action_id=command.action_id or None,
                    name=command.name,
                    arguments=command.arguments,
                )
                for command in commands
            ]
            snapshot = self._tool_snapshot()
            if snapshot is None:
                raise RuntimeError("tool action has no pinned binding snapshot")
            effects = {definition.name: definition.effect for definition in snapshot.catalog.definitions}
            checkpoint = any(
                entry.action_id is not None and effects.get(entry.name) == "external" for entry in executed
            )
            if checkpoint:
                call_projection = await channel.project_call(content, executed)
                self._require_applied(
                    await self._transaction.record_model_turn(
                        self._transaction.context(f"{operation_prefix}:model-turn"),
                        call_projection,
                    )
                )

            failed = False
            try:
                for entry in executed:
                    if failed:
                        entry.output = (
                            f"[SKIPPED] Command {entry.name} was not executed because an earlier "
                            "command failed. Please replan in the next round."
                        )
                        entry.success = False
                        entry.settled = True
                        continue
                    dispatched = await self._tool_execution_port.dispatch(
                        ToolDispatchRequest(
                            snapshot.snapshot_id,
                            snapshot.registry_revision,
                            entry.name,
                            entry.arguments,
                            entry.action_id or "",
                        )
                    )
                    if not dispatched.success:
                        entry.output = f"[TOOL BINDING CONFLICT] {dispatched.conflict}"
                        entry.success = False
                        entry.settled = True
                        failed = True
                        continue
                    result = dispatched.value
                    if result is None:
                        raise RuntimeError("successful tool dispatch has no result")
                    self._apply_result(entry, result)
                    failed = not result.success
                    if result.terminate:
                        self._set_active(False)
            except BaseException:
                if checkpoint:
                    for entry in executed:
                        if not entry.settled:
                            entry.output = "[INTERRUPTED] Command did not complete (the turn was interrupted)."
                            entry.success = False
                    projection = await channel.project_results(executed)
                    self._require_applied(
                        await self._transaction.record_tool_results(
                            self._transaction.context(f"{operation_prefix}:tool-results"),
                            (projection,),
                        )
                    )
                raise

            outputs = join_command_outputs(executed)
            if checkpoint:
                projection = await channel.project_results(executed)
                self._require_applied(
                    await self._transaction.record_tool_results(
                        self._transaction.context(f"{operation_prefix}:tool-results"),
                        (projection,),
                    )
                )
            else:
                projection = await channel.project_turn(content, executed)
                self._require_applied(
                    await self._transaction.record_tool_results(
                        self._transaction.context(f"{operation_prefix}:complete-turn"),
                        (projection,),
                    )
                )
            await self._inference_engine.join()
            return AIMessage(
                content=channel.react_result(outputs),
                sent_from=self._context().name,
                cause_by=CauseBy.RUN_COMMAND,
            )

    @staticmethod
    def _require_applied(result: MutationResult) -> None:
        if result.status not in {
            MutationStatus.APPLIED,
            MutationStatus.ALREADY_APPLIED,
        }:
            raise RuntimeError(result.reason or result.status.value)

    @staticmethod
    def _apply_result(entry: ExecutedCommand, result: ToolExecutionOutcome) -> None:
        entry.output = result.output
        entry.success = result.success
        entry.media = list(result.media)
        entry.artifacts = list(result.artifacts)
        entry.file_changes = list(result.file_changes)
        entry.retention = result.retention
        entry.resource_path = result.resource_path
        entry.data = result.data
        entry.settled = True


__all__ = ["ActionExecutionService"]
