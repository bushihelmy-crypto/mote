"""Receipt-only settlement for durable execution frontiers before Graph entry."""

from __future__ import annotations

from mote.contracts.conversation import ToolMessage
from mote.contracts.conversation.fields import TOOL_EFFECT_PRESENTATION_DIGEST, TOOL_EFFECT_RECEIPT_ID
from mote.contracts.events.conversation import MessageAppendedEvent
from mote.contracts.events.envelope import JsonValue
from mote.contracts.events.pending_act import PendingActionResultCommittedEvent
from mote.contracts.execution.restore import (
    ExecutionRestore,
    ExternalEffectReconciliationRequired,
    InterruptedExecutionNeedsSettlement,
)
from mote.contracts.ports.events.journal import StreamWriterFence
from mote.contracts.ports.execution.reconciliation import ExternalEffectResultQuery
from mote.contracts.tool.identity import ToolInvocationId
from mote.runtime.session.execution_restore import RuntimeExecutionRestore
from mote.runtime.session.pending_act import RuntimePendingActService
from mote.runtime.session.projection import SessionLiveProjection


class RuntimeExecutionReconciler:
    """Query existing receipts and commit facts without an invoke capability."""

    def __init__(
        self,
        projection: SessionLiveProjection,
        pending_act: RuntimePendingActService,
        external_results: ExternalEffectResultQuery | None = None,
    ) -> None:
        self._projection = projection
        self._pending_act = pending_act
        self._external_results = external_results

    def snapshot(self, run_id: str) -> ExecutionRestore:
        return RuntimeExecutionRestore(self._projection, run_id=run_id).snapshot()

    async def reconcile_started_external_effects(
        self,
        run_id: str,
        *,
        writer: StreamWriterFence,
    ) -> ExecutionRestore:
        restored = RuntimeExecutionRestore(self._projection, run_id=run_id).snapshot()
        if isinstance(restored, InterruptedExecutionNeedsSettlement):
            return restored
        if not isinstance(restored, ExternalEffectReconciliationRequired):
            return restored
        state = self._projection.snapshot()
        evidence_by_invocation: list[tuple[ToolInvocationId, JsonValue]] = []
        for invocation_id in restored.invocation_ids:
            identity = self._identity(state, invocation_id)
            action = next(action for action in restored.frontier.actions if action.invocation_id == invocation_id)
            result = (
                await self._external_results.query_external_effect_result(identity, action.tool_name)
                if self._external_results is not None
                else None
            )
            if result is not None:
                if result.receipt.identity != identity:
                    raise ValueError("external result query returned a different invocation identity")
                message = ToolMessage(content=result.output, tool_call_id=action.action_id)
                message.metadata[TOOL_EFFECT_RECEIPT_ID] = result.receipt.receipt_id
                message.metadata[TOOL_EFFECT_PRESENTATION_DIGEST] = result.receipt.presentation_digest
                current = self._projection.snapshot()
                await self._pending_act.commit_reconciled_external_result(
                    restored.frontier.frontier_id,
                    result.receipt,
                    MessageAppendedEvent(message),
                    PendingActionResultCommittedEvent(
                        restored.frontier.frontier_id,
                        invocation_id,
                        message.id,
                        result.receipt.receipt_id,
                        result.receipt.presentation_digest,
                    ),
                    expected_stream_version=current.through_sequence,
                    writer=writer,
                )
            else:
                evidence: JsonValue = {"reason": "external_receipt_query_returned_unknown"}
                evidence_by_invocation.append((invocation_id, evidence))
        if evidence_by_invocation:
            current = self._projection.snapshot()
            await self._pending_act.mark_external_effects_in_doubt(
                restored.frontier.frontier_id,
                tuple(evidence_by_invocation),
                expected_stream_version=current.through_sequence,
                writer=writer,
            )
        return RuntimeExecutionRestore(self._projection, run_id=run_id).snapshot()

    @staticmethod
    def _identity(state, invocation_id: ToolInvocationId):
        effect = state.external_effect_by_invocation.get(invocation_id)
        if effect is None:
            raise ValueError("reconciliation invocation has no external effect")
        # The complete identity is carried by ExternalEffectStartedEvent but the
        # projection intentionally exposed only state. This invariant is closed by
        # retaining that identity in the canonical projection.
        identity = state.external_effect_identity_by_invocation.get(invocation_id)
        if identity is None:
            raise ValueError("external effect projection omitted invocation identity")
        return identity


__all__ = ["RuntimeExecutionReconciler"]
