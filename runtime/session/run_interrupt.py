"""Two-phase Session-owned user interrupt commands."""

from __future__ import annotations

from datetime import datetime

from mote.contracts.events.pending_act import (
    ExternalEffectFinishedEvent,
    ExternalEffectInDoubtEvent,
    PendingActInterruptedEvent,
    PendingActionResultCommittedEvent,
    RunRecoveryCursorAdvancedEvent,
    TurnInterruptedContextAttachedEvent,
    TurnInterruptedEvent,
    TurnInterruptSettledEvent,
)
from mote.contracts.execution.interrupt import RunInterruptPermit
from mote.contracts.execution.interrupt_context import TURN_ABORTED_FRAGMENT
from mote.contracts.execution.run_cursor import RecoveryTarget, RunRecoveryCursor
from mote.contracts.ports.events.journal import StreamWriterFence
from mote.contracts.ports.session.facts import GuardedSessionFactBatch, GuardedSessionFactSink, RolloutSourceEvent
from mote.contracts.tool.external_effect import ToolEffectReceipt
from mote.contracts.tool.identity import ToolInvocationId
from mote.runtime.session.projection import SessionLiveProjection


class RunInterruptService:
    def __init__(self, projection: SessionLiveProjection, sink: GuardedSessionFactSink) -> None:
        self._projection = projection
        self._sink = sink

    async def interrupt_run(
        self,
        run_id: str,
        *,
        model_call_id: str | None,
        interrupted_at: datetime,
        expected_stream_version: int,
        writer: StreamWriterFence,
    ) -> RunInterruptPermit:
        state = self._projection.snapshot()
        if state.through_sequence != expected_stream_version:
            raise ValueError("interrupt snapshot is not at expected stream version")
        prior = state.interrupted_run_by_id.get(run_id)
        if prior is not None:
            return RunInterruptPermit(
                run_id,
                writer.owner_id,
                writer.incarnation_id,
                writer.fencing_token,
                prior.interrupted_at,
            )
        await self._sink.commit_guarded(
            GuardedSessionFactBatch(
                (TurnInterruptedEvent(run_id, model_call_id, "user_interrupted", interrupted_at),),
                expected_stream_version,
                writer,
            )
        )
        return RunInterruptPermit(
            run_id,
            writer.owner_id,
            writer.incarnation_id,
            writer.fencing_token,
            interrupted_at,
        )

    async def settle(
        self,
        permit: RunInterruptPermit,
        *,
        anchor_message_id: str,
        expected_stream_version: int,
        writer: StreamWriterFence,
        result_events: tuple[RolloutSourceEvent, ...] = (),
        action_results: tuple[PendingActionResultCommittedEvent, ...] = (),
        effect_receipts: tuple[ToolEffectReceipt, ...] = (),
        in_doubt_external_invocations: tuple[ToolInvocationId, ...] = (),
    ) -> None:
        state = self._projection.snapshot()
        if state.through_sequence != expected_stream_version:
            raise ValueError("interrupt settlement snapshot is stale")
        if permit.run_id in state.settled_interrupt_runs:
            return
        if permit.run_id not in state.interrupted_run_by_id:
            raise ValueError("interrupt settlement has no durable interrupt")
        events = []
        frontier_id = state.active_pending_act_by_run.get(permit.run_id)
        if frontier_id is not None:
            frontier = state.pending_act_by_id[frontier_id]
            accounted = {
                invocation
                for invocation in state.pending_action_result_by_invocation
                if invocation in {action.invocation_id for action in frontier.actions}
            } | {
                invocation
                for invocation in state.skipped_pending_actions
                if invocation in {action.invocation_id for action in frontier.actions}
            }
            new_results = {event.invocation_id for event in action_results}
            if accounted | new_results != {action.invocation_id for action in frontier.actions}:
                raise ValueError("interrupt settlement must account for every action")
            for receipt in effect_receipts:
                effect = state.external_effect_by_invocation.get(receipt.identity.invocation_id)
                if effect is None or effect.state.value != "started":
                    raise ValueError("interrupt receipt requires a STARTED external effect")
            for invocation_id in in_doubt_external_invocations:
                effect = state.external_effect_by_invocation.get(invocation_id)
                if effect is None or effect.state.value != "started":
                    raise ValueError("interrupt in-doubt settlement requires a STARTED external effect")
            events.extend(
                tuple(ExternalEffectFinishedEvent(frontier_id, receipt) for receipt in effect_receipts)
                + tuple(
                    ExternalEffectInDoubtEvent(
                        frontier_id,
                        invocation_id,
                        {"reason": "interrupt_receipt_query_returned_unknown"},
                    )
                    for invocation_id in in_doubt_external_invocations
                )
                + result_events
                + action_results
                + (
                    PendingActInterruptedEvent(frontier_id, frontier.revision),
                    RunRecoveryCursorAdvancedEvent(
                        RunRecoveryCursor(
                            permit.run_id,
                            state.run_cursor_by_run_id[permit.run_id].revision + 1,
                            RecoveryTarget.OBSERVE,
                            None,
                            False,
                        )
                    ),
                )
            )
        events.extend(
            (
                TurnInterruptedContextAttachedEvent(permit.run_id, anchor_message_id),
                TurnInterruptSettledEvent(permit.run_id),
            )
        )
        await self._sink.commit_guarded(GuardedSessionFactBatch(tuple(events), expected_stream_version, writer))


__all__ = ["RunInterruptService", "TURN_ABORTED_FRAGMENT"]
