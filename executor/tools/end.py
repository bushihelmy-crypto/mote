"""End command — terminate current session and produce summary."""
from __future__ import annotations

from typing import Callable, Awaitable

from metagpt.executor.base_tool import BaseTool
from metagpt.executor.tool_registry import register_tool


@register_tool
class End(BaseTool):
    """End the current session and produce a summary."""

    name = "End"
    description = "End the current task and produce a final summary."
    requires = ("end_session",)

    # Injected from Role by bind(): Role.end_session.
    end_session: Callable[[], Awaitable[str]]

    async def call(self) -> str:
        """End the current session, generate summary if configured."""
        return await self.end_session()
