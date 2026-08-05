"""Provider-independent actions emitted by a model turn."""

from __future__ import annotations

from typing import Any, Literal, Union

from pydantic import BaseModel, Field, field_validator

from mote.contracts.tool.arguments import ToolArguments, freeze_tool_arguments


class TextAction(BaseModel):
    kind: Literal["text"] = "text"
    content: str = ""


class ToolCallAction(BaseModel):
    kind: Literal["tool_call"] = "tool_call"
    action_id: str = ""
    name: str
    arguments: ToolArguments = Field(default_factory=dict)

    @field_validator("arguments")
    @classmethod
    def _freeze_arguments(cls, value: object) -> ToolArguments:
        return freeze_tool_arguments(value)


class FinalCandidateAction(BaseModel):
    kind: Literal["final_candidate"] = "final_candidate"
    candidate_id: str = ""
    raw: Any = None
    representation: str


AgentAction = Union[TextAction, ToolCallAction, FinalCandidateAction]


__all__ = ["AgentAction", "FinalCandidateAction", "TextAction", "ToolCallAction"]
