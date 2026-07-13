"""End command — terminate the current session."""
from __future__ import annotations

from mote.common.prompt.tools import END_DESCRIPTION
from mote.executor.base_tool import BaseTool
from mote.executor.capability_types import EndSession
from mote.executor.tool_registry import register_tool


@register_tool
class End(BaseTool):
    """End the current session."""

    name = "End"
    description = END_DESCRIPTION
    requires = ("end_session",)

    # Injected from Role by bind(): Role.end_session.
    end_session: EndSession

    async def call(self) -> str:
        """End the current session (deactivates the Role)."""
        return await self.end_session()
