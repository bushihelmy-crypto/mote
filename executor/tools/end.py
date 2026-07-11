"""End command — terminate current session and produce summary."""
from __future__ import annotations

from typing import Awaitable, Callable

from mote.common.prompt.tools import END_DESCRIPTION
from mote.executor.base_tool import BaseTool
from mote.executor.tool_registry import register_tool


@register_tool
class End(BaseTool):
    """End the current session and produce a summary."""

    name = "End"
    description = END_DESCRIPTION
    requires = ("end_session",)

    # Injected from Role by bind(): Role.end_session.
    end_session: Callable[[], Awaitable[str]]

    async def call(self) -> str:
        """End the current session, generate summary if configured."""
        return await self.end_session()
