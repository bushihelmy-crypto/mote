"""End command — terminate the current session."""

from __future__ import annotations

from mote.runtime.tools.base_tool import BaseTool
from mote.runtime.tools.capability_types import EndSession


class End(BaseTool):
    """End the current session."""

    name = "End"
    requires = ("end_session",)

    # Injected from Role by bind(): Role.end_session.
    end_session: EndSession

    async def call(self) -> str:
        """End the current task — your last message stands as the final reply.

        Deactivates the Role. Call this when the task is complete; whatever you
        said in your last message is delivered to the user as the final reply.
        """
        return await self.end_session()
