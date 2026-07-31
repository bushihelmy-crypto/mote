"""Request-only instructions from active dynamic Toolset views."""

from __future__ import annotations

from typing import Callable, Optional, Protocol

from mote.contracts.ports.conversation.turn_context import TurnContextPriority


class _InstructionExecutor(Protocol):
    def dynamic_toolset_instructions(self) -> tuple[str, ...]:
        ...


class ToolsetInstructionsContextSource:
    """Render current run/step Toolset instructions without persisting them."""

    name = "toolset_instructions"
    priority = TurnContextPriority.TOOL_INSTRUCTIONS
    save_to_context = False

    def __init__(
        self,
        get_executor: Callable[[], Optional[_InstructionExecutor]],
    ) -> None:
        self._get_executor = get_executor

    async def render(self, *, cwd: Optional[str] = None) -> Optional[str]:
        executor = self._get_executor()
        if executor is None:
            return None
        instructions = executor.dynamic_toolset_instructions()
        if not instructions:
            return None
        return "# Toolset instructions\n" + "\n\n".join(instructions)


__all__ = ["ToolsetInstructionsContextSource"]
