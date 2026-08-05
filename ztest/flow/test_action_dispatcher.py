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
    assert ActionDispatcher().tool_commands(turn, {"Read"}) == [
        ToolCallAction(action_id="c1", name="Read", arguments={"path": "a.py"})
    ]


def test_filters_unknown_tool_names():
    turn = ModelTurn(actions=[ToolCallAction(name="Unknown")])
    assert ActionDispatcher().tool_commands(turn, {"Read"}) == []
