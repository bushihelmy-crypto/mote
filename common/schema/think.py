"""Think engine result type — consolidated from think/think_engine.py."""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field


class ThinkResult(BaseModel):
    """The outcome of one think round, free of any message/role semantics.

    ThinkEngine produces this domain object; callers (e.g. Role) decide how to
    wrap it — for example as an AIMessage — so the engine never owns the role
    semantics of its output.

    ``content`` is the assistant's text. ``tool_calls`` is the structured IR
    from the native channel; ``None`` means the round ran in the XML text
    channel (where commands live in ``content`` and are parsed downstream).
    """

    content: str = Field(default="")
    tool_calls: Optional[list[dict]] = Field(default=None)

    @property
    def is_native(self) -> bool:
        """True if this round used the native tool-use channel."""
        return self.tool_calls is not None

    @property
    def is_empty(self) -> bool:
        """True if there is no assistant text for this round."""
        return not self.content
