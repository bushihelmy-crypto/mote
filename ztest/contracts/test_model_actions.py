from mote.contracts import FinalCandidateAction, ModelTurn, TextAction, ToolCallAction, ToolEffect


def test_model_turn_round_trip_uses_stable_discriminators() -> None:
    turn = ModelTurn(
        content="working",
        actions=[
            TextAction(content="working"),
            ToolCallAction(action_id="call-1", name="Read", arguments={"path": "README.md"}),
            FinalCandidateAction(candidate_id="final-1", raw={"ok": True}, representation="native"),
        ],
    )

    restored = ModelTurn.model_validate_json(turn.model_dump_json())
    assert [action.kind for action in restored.actions] == ["text", "tool_call", "final_candidate"]
    assert restored == turn


def test_tool_effect_values_are_persistence_stable() -> None:
    assert [effect.value for effect in ToolEffect] == ["pure", "local", "external"]
