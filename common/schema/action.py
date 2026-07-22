"""Provider-independent semantic actions produced by one model turn."""
from __future__ import annotations

from typing import Any, Literal, Union

from pydantic import BaseModel, Field


class TextAction(BaseModel):
    kind: Literal["text"] = "text"
    content: str = ""


class ToolCallAction(BaseModel):
    kind: Literal["tool_call"] = "tool_call"
    action_id: str = ""
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class FinalCandidateAction(BaseModel):
    kind: Literal["final_candidate"] = "final_candidate"
    candidate_id: str = ""
    raw: Any = None
    representation: str


AgentAction = Union[TextAction, ToolCallAction, FinalCandidateAction]


class ModelTurn(BaseModel):
    """One model response normalized away from its provider wire format."""

    content: str = ""
    actions: list[AgentAction] = Field(default_factory=list)

    @property
    def final_candidates(self) -> list[FinalCandidateAction]:
        return [action for action in self.actions if isinstance(action, FinalCandidateAction)]
