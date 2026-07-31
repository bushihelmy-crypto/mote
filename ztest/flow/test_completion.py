import pytest

from mote.contracts.model.turn import FinalCandidateAction, ModelTurn, TextAction, ToolCallAction
from mote.contracts.output.completion import CompletionKind
from mote.kernel.execution.operations.completion import TextCompletionPolicy

pytestmark = pytest.mark.asyncio


async def test_text_without_final_candidate_continues():
    turn = ModelTurn(content="thinking", actions=[TextAction(content="thinking")])
    assert (await TextCompletionPolicy().evaluate(turn)).kind is CompletionKind.CONTINUE


async def test_one_final_candidate_completes():
    turn = ModelTurn(actions=[FinalCandidateAction(raw="done", representation="native_text")])
    assert (await TextCompletionPolicy().evaluate(turn)).kind is CompletionKind.VALIDATE_CANDIDATE


async def test_multiple_final_candidates_fail_closed():
    turn = ModelTurn(
        actions=[
            FinalCandidateAction(raw="a", representation="test"),
            FinalCandidateAction(raw="b", representation="test"),
        ]
    )
    assert (await TextCompletionPolicy().evaluate(turn)).kind is CompletionKind.FAIL


async def test_final_candidate_cannot_share_turn_with_tool_call():
    turn = ModelTurn(
        actions=[
            ToolCallAction(name="Read"),
            FinalCandidateAction(raw="done", representation="test"),
        ]
    )
    assert (await TextCompletionPolicy().evaluate(turn)).kind is CompletionKind.FAIL
