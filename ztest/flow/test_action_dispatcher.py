from mote.contracts.model.turn import FinalCandidateAction, ModelTurn, TextAction, ToolCallAction
from mote.kernel.execution.operations.actions import ActionDispatcher


def test_projects_only_tool_actions():
    turn = ModelTurn(
        actions=[
            TextAction(content="thinking"),
            ToolCallAction(action_id="c1", name="Read", arguments={"path": "a.py"}),
            FinalCandidateAction(raw="ignored", representation="test"),
        ]
    )
    assert ActionDispatcher().tool_commands(turn) == [
        ToolCallAction(action_id="c1", name="Read", arguments={"path": "a.py"})
    ]


def test_preserves_unknown_tool_names_for_snapshot_validation():
    turn = ModelTurn(actions=[ToolCallAction(name="Unknown")])
    assert ActionDispatcher().tool_commands(turn) == [ToolCallAction(name="Unknown")]
