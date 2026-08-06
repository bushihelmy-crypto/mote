"""Transactional execution of one model-emitted tool batch."""

from __future__ import annotations

import hashlib
from collections.abc import Callable

from mote.contracts.conversation import AIMessage, CauseBy, Message
from mote.contracts.conversation.fields import TOOL_EFFECT_PRESENTATION_DIGEST, TOOL_EFFECT_RECEIPT_ID
from mote.contracts.events.envelope import thaw_json
from mote.contracts.events.pending_act import PendingActionResultCommittedEvent, PendingActionsSkippedEvent
from mote.contracts.execution.models import MutationResult, MutationStatus
from mote.contracts.execution.pending_act import PendingActFrontier
from mote.contracts.model.inference import InferenceResult
from mote.contracts.model.turn import ModelTurn
from mote.contracts.ports.execution.pending_act import PendingActAcceptance, PendingActAcceptancePort
from mote.contracts.ports.execution.transaction import ExecutionTransactionPort
from mote.contracts.ports.tool.approval import ToolApprovalIntent
from mote.contracts.tool.catalog import (
    ToolBindingSnapshot,
    ToolDispatchRequest,
    ToolExecutionOutcome,
    ToolExecutionPort,
)
from mote.contracts.tool.effects import ToolEffect
from mote.contracts.tool.errors import ToolNotFoundError
from mote.contracts.tool.external_effect import ToolEffectReceipt
from mote.contracts.tool.result import JsonToolPayload
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
        pending_act_acceptance: PendingActAcceptancePort,
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
        self._pending_act_acceptance = pending_act_acceptance
        self._report_think_result = report_inference_result
        self._set_active = set_active
        self._dispatcher = dispatcher or ActionDispatcher()

    async def execute(self, turn: ModelTurn | None = None) -> Message:
        async with span("act"):
            if turn is None:
                turn = await self._channel().model_turn(self._inference_engine.result)
            return await self._drive(turn, acceptance=None)

    async def resume(self, frontier: PendingActFrontier) -> Message:
        async with span("act"):
            snapshot = self._tool_snapshot()
            if snapshot is None:
                raise RuntimeError("PendingAct resume has no pinned binding snapshot")
            recovered = self._pending_act_acceptance.resume(frontier, snapshot)
            return await self._drive(
                ModelTurn(content="", actions=list(recovered.actions)),
                acceptance=PendingActAcceptance(recovered.frontier),
                already_settled=(recovered.completed_invocation_ids | recovered.skipped_invocation_ids),
                recovered_messages=recovered.committed_result_messages,
            )

    async def _drive(
        self,
        turn: ModelTurn,
        *,
        acceptance: PendingActAcceptance | None,
        already_settled: frozenset[str] = frozenset(),
        recovered_messages: tuple[Message, ...] = (),
    ) -> Message:
        channel = self._channel()
        commands = self._dispatcher.tool_commands(turn)
        content = (self._inference_engine.result.content or "") if acceptance is None else ""
        operation_prefix = self._inference_engine.model_call_id or "inference"
        if acceptance is None:
            self._report_think_result(self._inference_engine.result)

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
        available_names = {item.name for item in snapshot.catalog.definitions}
        unavailable_names = tuple(
            dict.fromkeys(command.name for command in commands if command.name not in available_names)
        )
        if unavailable_names:
            rendered = ", ".join(unavailable_names)
            raise ToolNotFoundError(f"tool not found or expired from the pinned snapshot: {rendered}")
        if commands and acceptance is None:
            call_projection = await channel.project_call(content, executed)
            acceptance = await self._pending_act_acceptance.accept(tuple(commands), snapshot, call_projection.messages)
        effects = {item.name: ToolEffect(item.effect) for item in snapshot.catalog.definitions}
        self._tool_execution_port.bind_approval_coordinator(
            _PendingActApproval(self._pending_act_acceptance, acceptance) if acceptance is not None else None
        )
        effect_receipts_by_invocation: dict[str, ToolEffectReceipt] = {}
        rejected_approval_request_id = None
        failed = False
        for ordinal, entry in enumerate(executed):
            if acceptance is not None and acceptance.frontier.actions[ordinal].invocation_id.value in already_settled:
                entry.output = "[RECOVERED] Durable tool result was already committed."
                entry.success = True
                entry.settled = True
                continue
            if failed:
                entry.output = (
                    f"[SKIPPED] Command {entry.name} was not executed because an earlier "
                    "command failed. Please replan in the next round."
                )
                entry.success = False
                entry.settled = True
                continue
            request = ToolDispatchRequest(
                snapshot.snapshot_id,
                snapshot.registry_revision,
                entry.name,
                entry.arguments,
                entry.action_id or "",
            )
            effect_permit = None
            if acceptance is not None:
                authorized = await self._tool_execution_port.authorize(request)
                if not authorized.success:
                    entry.output = authorized.conflict
                    entry.success = False
                    entry.settled = True
                    failed = True
                    rejected_approval_request_id = authorized.approval_request_id
                    continue
                identity = self._tool_execution_port.invocation_identity(request)
                invoke_permit = await self._pending_act_acceptance.begin_invoke(acceptance, ordinal, identity)
                self._tool_execution_port.bind_fileops_transaction(request, invoke_permit.fileops_transaction_id)
            if acceptance is not None and effects.get(entry.name) is ToolEffect.EXTERNAL:
                effect_permit = await self._pending_act_acceptance.begin_external_effect(
                    acceptance,
                    ordinal,
                    identity,
                )
            try:
                dispatched = await self._tool_execution_port.dispatch(request)
            except BaseException as error:
                if effect_permit is not None:
                    await self._pending_act_acceptance.mark_external_effect_in_doubt(
                        effect_permit,
                        evidence={
                            "reason": "dispatch_raised_after_external_effect_started",
                            "exception_type": type(error).__name__,
                        },
                    )
                raise
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
            if effect_permit is not None:
                effect_receipts_by_invocation[effect_permit.identity.invocation_id.value] = ToolEffectReceipt(
                    receipt_id=f"tool-result:{effect_permit.identity.invocation_id.value}",
                    identity=effect_permit.identity,
                    disposition="succeeded" if result.success else "failed",
                    provider_evidence={"dispatch_completed": True},
                    artifacts=tuple(result.artifacts),
                    media=tuple(result.media),
                    file_changes=tuple(result.file_changes),
                    presentation_digest=_presentation_digest(result.output),
                )
            failed = not result.success
            if result.terminate:
                self._set_active(False)
        outputs = join_command_outputs(executed)
        settled_action_ids = (
            {
                action.action_id
                for action in acceptance.frontier.actions
                if action.invocation_id.value in already_settled
            }
            if acceptance is not None
            else set()
        )
        unsettled_executed = [entry for entry in executed if entry.action_id not in settled_action_ids]
        projection = await channel.project_results(unsettled_executed)
        receipts_by_action = (
            {
                action.action_id: effect_receipts_by_invocation[action.invocation_id.value]
                for action in acceptance.frontier.actions
                if action.invocation_id.value in effect_receipts_by_invocation
            }
            if acceptance is not None
            else {}
        )
        for message in projection.messages:
            action_id = message.metadata.get("tool_call_id")
            receipt = receipts_by_action.get(action_id) if isinstance(action_id, str) else None
            if receipt is not None:
                message.metadata[TOOL_EFFECT_RECEIPT_ID] = receipt.receipt_id
                message.metadata[TOOL_EFFECT_PRESENTATION_DIGEST] = receipt.presentation_digest
        if acceptance is not None:
            settlement_messages = projection.messages
            messages_by_action = {
                message.metadata.get("tool_call_id"): message
                for message in (*recovered_messages, *settlement_messages)
                if isinstance(message.metadata.get("tool_call_id"), str)
            }
            skipped_ids = tuple(
                action.invocation_id
                for action in acceptance.frontier.actions
                if executed[action.ordinal].output.startswith("[SKIPPED]")
            )
            skipped_id_set = set(skipped_ids)
            action_results = tuple(
                PendingActionResultCommittedEvent(
                    acceptance.frontier.frontier_id,
                    action.invocation_id,
                    messages_by_action[action.action_id].id,
                    (
                        receipts_by_action[action.action_id].receipt_id
                        if action.action_id in receipts_by_action
                        else None
                    ),
                    (
                        receipts_by_action[action.action_id].presentation_digest
                        if action.action_id in receipts_by_action
                        else None
                    ),
                )
                for action in acceptance.frontier.actions
                if action.action_id in messages_by_action
                and action.invocation_id.value not in already_settled
                and action.invocation_id not in skipped_id_set
            )
            await self._pending_act_acceptance.settle(
                acceptance,
                settlement_messages,
                continue_inference=True,
                effect_receipts=tuple(effect_receipts_by_invocation.values()),
                action_results=action_results,
                skipped=(
                    PendingActionsSkippedEvent(
                        acceptance.frontier.frontier_id,
                        skipped_ids,
                        "earlier_action_failed",
                    )
                    if skipped_ids
                    else None
                ),
                rejected_approval_request_id=rejected_approval_request_id,
            )
        else:
            self._require_applied(
                await self._transaction.settle_effect_batch(
                    self._transaction.context(f"{operation_prefix}:tool-results"),
                    projection,
                )
            )
        if (
            acceptance is None or acceptance.frontier.model_call_id == self._inference_engine.model_call_id
        ) and not self._inference_engine.done:
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
        entry.data = (
            thaw_json(result.payload.value) if isinstance(result.payload, JsonToolPayload) else result.execution_value
        )
        entry.settled = True


def _presentation_digest(content: str) -> str:
    return f"sha256-{hashlib.sha256(content.encode('utf-8')).hexdigest()}"


class _PendingActApproval:
    def __init__(self, port: PendingActAcceptancePort, acceptance: PendingActAcceptance) -> None:
        self._port = port
        self._acceptance = acceptance

    async def resolve(self, intent: ToolApprovalIntent):
        return await self._port.resolve_approval(self._acceptance, intent)


__all__ = ["ActionExecutionService"]
