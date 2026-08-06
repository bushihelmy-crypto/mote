from __future__ import annotations

import pytest

from mote.contracts.conversation import AIMessage
from mote.contracts.execution.restore import CommittedExecution, ObserveExecution, PendingActExecution
from mote.contracts.execution.run_cursor import RecoveryTarget, RunRecoveryCursor
from mote.contracts.output import CommittedOutput
from mote.kernel.execution.graph.core import End, NodeId
from mote.kernel.execution.graph.nodes import RestoreNode
from mote.kernel.execution.state import ExecutionState, RecoveredPendingAct
from ztest.runtime.test_execution_restore import _frontier

pytestmark = pytest.mark.asyncio


class _Restore:
    def __init__(self, value):
        self.value = value

    def snapshot(self):
        return self.value


async def test_act_cursor_enters_act_without_output_or_inference_call() -> None:
    frontier = _frontier()
    cursor = RunRecoveryCursor(frontier.run_id, 0, RecoveryTarget.ACT, frontier.frontier_id, False)
    node = RestoreNode(
        _Restore(PendingActExecution(frontier, cursor)),
        allow_pending_act=True,
    )
    state = ExecutionState(response=AIMessage(content=""))

    transition = await node.run(state)

    assert transition.target is NodeId.ACT
    assert isinstance(state.turn, RecoveredPendingAct)
    assert state.turn.frontier is frontier


async def test_observe_cursor_restores_continue_inference() -> None:
    cursor = RunRecoveryCursor("run", 1, RecoveryTarget.OBSERVE, None, True)
    node = RestoreNode(_Restore(ObserveExecution(cursor)), allow_pending_act=True)
    state = ExecutionState(response=AIMessage(content=""))

    transition = await node.run(state)

    assert transition.target is NodeId.OBSERVE
    assert state.initial_observe_complete is True
    assert state.continue_inference is True


async def test_committed_execution_ends_without_secondary_output_query() -> None:
    presentation = AIMessage(content="done")
    committed = CommittedOutput("candidate", "contract", "schema", "value")
    node = RestoreNode(
        _Restore(CommittedExecution(committed, presentation)),
        allow_pending_act=True,
    )

    transition = await node.run(ExecutionState(response=AIMessage(content="")))

    assert isinstance(transition, End)
    assert transition.result is not None
    assert transition.result.presentation is presentation
    assert transition.result.committed_output is committed
