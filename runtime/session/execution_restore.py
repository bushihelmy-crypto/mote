"""Single-snapshot resolver for the Session-owned execution cursor."""

from __future__ import annotations

from typing import Generic, TypeVar

from mote.contracts.execution.restore import (
    ExecutionRestore,
    ExternalEffectReconciliationRequired,
    InDoubtExecution,
    InterruptedExecution,
    InterruptedExecutionNeedsSettlement,
    NoPendingExecution,
    ObserveExecution,
    PendingActExecution,
    UnrecoverablePreV1Execution,
)
from mote.contracts.execution.run_cursor import RecoveryTarget
from mote.contracts.ports.execution.output_restore import CommittedExecutionQuery
from mote.contracts.tool.external_effect import ExternalEffectState
from mote.runtime.session.projection import SessionLiveProjection

OutputT = TypeVar("OutputT")


class RuntimeExecutionRestore(Generic[OutputT]):
    def __init__(
        self,
        projection: SessionLiveProjection,
        *,
        run_id: str,
        committed_output: CommittedExecutionQuery[OutputT] | None = None,
    ) -> None:
        if not run_id:
            raise ValueError("execution restore requires a run identity")
        self._projection = projection
        self._run_id = run_id
        self._committed_output = committed_output

    def snapshot(self) -> ExecutionRestore[OutputT]:
        state = self._projection.snapshot()
        if self._committed_output is not None:
            terminal = self._committed_output.restored_committed_execution()
        else:
            terminal = None
        if terminal is not None:
            if self._run_id in state.active_pending_act_by_run:
                raise ValueError("committed output conflicts with an active PendingAct")
            return terminal
        if self._run_id in state.interrupted_run_by_id:
            if (
                self._run_id not in state.pending_act_schema_activated_runs
                and self._run_id not in state.run_cursor_by_run_id
            ):
                return UnrecoverablePreV1Execution(self._run_id)
            if self._run_id in state.settled_interrupt_runs:
                return InterruptedExecution(self._run_id)
            return InterruptedExecutionNeedsSettlement(self._run_id)
        cursor = state.run_cursor_by_run_id.get(self._run_id)
        if cursor is None:
            return NoPendingExecution()
        if cursor.next_node is RecoveryTarget.OBSERVE:
            return ObserveExecution(cursor)
        frontier_id = cursor.pending_act_id
        if frontier_id is None:
            raise ValueError("ACT cursor omitted its PendingAct identity")
        frontier = state.pending_act_by_id.get(frontier_id)
        if frontier is None or state.active_pending_act_by_run.get(self._run_id) != frontier_id:
            raise ValueError("ACT cursor does not reference the active PendingAct")
        reconciliation_required = tuple(
            action.invocation_id
            for action in frontier.actions
            if (
                (effect := state.external_effect_by_invocation.get(action.invocation_id)) is not None
                and effect.state is ExternalEffectState.STARTED
            )
        )
        if reconciliation_required:
            return ExternalEffectReconciliationRequired(frontier, reconciliation_required)
        in_doubt = tuple(
            action.invocation_id
            for action in frontier.actions
            if (
                (effect := state.external_effect_by_invocation.get(action.invocation_id)) is not None
                and effect.state is ExternalEffectState.IN_DOUBT
            )
        )
        if in_doubt:
            return InDoubtExecution(frontier, in_doubt)
        return PendingActExecution(frontier, cursor)


__all__ = ["RuntimeExecutionRestore"]
