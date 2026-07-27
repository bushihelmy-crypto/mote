"""Projection from semantic model actions to ordinary tool commands."""
from mote.contracts.model_actions import ModelTurn, ToolCallAction


class ActionDispatcher:
    """Select executable tool actions without knowing their wire representation."""

    def tool_commands(self, turn: ModelTurn, valid_names: set[str]) -> list[dict]:
        commands = []
        for action in turn.actions:
            if not isinstance(action, ToolCallAction):
                continue
            if valid_names and action.name not in valid_names:
                continue
            commands.append(
                {
                    "id": action.action_id or None,
                    "command_name": action.name,
                    "args": action.arguments,
                    "status": "running",
                    "error_msg": "",
                }
            )
        return commands
