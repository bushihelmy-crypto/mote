"""Projection from semantic model actions to ordinary tool commands."""
from mote.contracts.model.turn import ModelTurn, ToolCallAction


class ActionDispatcher:
    """Select executable tool actions without knowing their wire representation."""

    def tool_commands(self, turn: ModelTurn, valid_names: set[str]) -> list[ToolCallAction]:
        commands: list[ToolCallAction] = []
        for action in turn.actions:
            if not isinstance(action, ToolCallAction):
                continue
            if valid_names and action.name not in valid_names:
                continue
            commands.append(action)
        return commands
