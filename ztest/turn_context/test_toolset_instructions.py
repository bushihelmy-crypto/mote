from __future__ import annotations

import asyncio

from mote.contracts.ports import EphemeralContextSource
from mote.runtime.context.turn_context import ToolsetInstructionsContextSource


class _Executor:
    def __init__(self, instructions: tuple[str, ...] = ()) -> None:
        self.instructions = instructions

    def dynamic_toolset_instructions(self) -> tuple[str, ...]:
        return self.instructions


def test_dynamic_toolset_instructions_are_request_only_context() -> None:
    executor = _Executor(("Use the active tenant scope.", "Do not cross mounts."))
    source = ToolsetInstructionsContextSource(lambda: executor)

    assert isinstance(source, EphemeralContextSource)
    assert source.save_to_context is False
    rendered = asyncio.run(source.render())
    assert rendered == ("# Toolset instructions\n" "Use the active tenant scope.\n\n" "Do not cross mounts.")


def test_dynamic_toolset_instructions_source_is_silent_without_active_content() -> None:
    source = ToolsetInstructionsContextSource(lambda: _Executor())
    assert asyncio.run(source.render()) is None
