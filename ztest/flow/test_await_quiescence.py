from __future__ import annotations

import asyncio

import pytest

from mote.contracts.conversation import AIMessage, MessageQueue, UserMessage
from mote.contracts.model.turn import FinalCandidateAction, ModelTurn
from mote.kernel.execution.graph.core import End, NodeId, Transition
from mote.kernel.execution.graph.nodes import AwaitQuiescenceNode
from mote.kernel.execution.state import ExecutionState, NoModelTurn, PendingCandidate


@pytest.mark.asyncio
async def test_message_activity_generation_does_not_regress_after_drain() -> None:
    queue = MessageQueue()
    initial = queue.activity_snapshot()
    queue.push(UserMessage(content="one"))
    pushed = queue.activity_snapshot()
    queue.pop_all()
    drained = queue.activity_snapshot()

    assert initial.generation == 0
    assert pushed.generation == drained.generation == 1
    assert pushed.pending is True
    assert drained.pending is False


@pytest.mark.asyncio
async def test_wait_for_activity_cannot_miss_a_push() -> None:
    queue = MessageQueue()
    before = queue.activity_snapshot()
    waiter = asyncio.create_task(queue.wait_for_activity(before.generation))
    queue.push(UserMessage(content="wake"))

    observed = await asyncio.wait_for(waiter, timeout=1)
    assert observed.generation == before.generation + 1
    assert observed.pending is True


def test_drain_lease_release_restores_messages_in_order() -> None:
    queue = MessageQueue()
    first = UserMessage(content="first")
    second = UserMessage(content="second")
    queue.push(first)
    queue.push(second)

    lease = queue.reserve()
    queue.release(lease)

    assert queue.pop_all() == [first, second]


@pytest.mark.asyncio
async def test_quiescent_candidate_advances_to_validation() -> None:
    queue = MessageQueue()
    turn = ModelTurn(actions=[FinalCandidateAction(raw="done", representation="text")])
    state = ExecutionState[str](AIMessage(content=""), turn=PendingCandidate(turn, 0))
    node = AwaitQuiescenceNode(lambda: queue, lambda: None)

    outcome = await node.run(state)
    assert outcome == Transition(NodeId.VALIDATE_OUTPUT)


@pytest.mark.asyncio
async def test_inbox_activity_invalidates_pending_candidate() -> None:
    queue = MessageQueue()
    queue.push(UserMessage(content="new input"))
    turn = ModelTurn(actions=[FinalCandidateAction(raw="stale", representation="text")])
    state = ExecutionState[str](AIMessage(content=""), turn=PendingCandidate(turn, 0))
    node = AwaitQuiescenceNode(lambda: queue, lambda: None)

    outcome = await node.run(state)
    assert outcome == Transition(NodeId.OBSERVE)
    assert isinstance(state.turn, NoModelTurn)
    assert state.requested_end is None


@pytest.mark.asyncio
async def test_quiescent_run_without_candidate_ends() -> None:
    queue = MessageQueue()
    state = ExecutionState[str](AIMessage(content=""))
    node = AwaitQuiescenceNode(lambda: queue, lambda: None)

    outcome = await node.run(state)
    assert outcome == End(None)
