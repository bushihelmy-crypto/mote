"""Projection from semantic model actions to ordinary tool commands."""

from mote.contracts.model.turn import ModelTurn, ToolCallAction


class ActionDispatcher:
    """Select executable tool actions without knowing their wire representation."""

    def tool_commands(self, turn: ModelTurn) -> list[ToolCallAction]:
        return [action for action in turn.actions if isinstance(action, ToolCallAction)]
