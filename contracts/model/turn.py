"""One provider-independent model turn."""

from pydantic import BaseModel, Field

from mote.contracts.tool.actions import AgentAction, FinalCandidateAction, TextAction, ToolCallAction


class ModelTurn(BaseModel):
    content: str = ""
    actions: list[AgentAction] = Field(default_factory=list)

    @property
    def final_candidates(self) -> list[FinalCandidateAction]:
        return [action for action in self.actions if isinstance(action, FinalCandidateAction)]


__all__ = ["AgentAction", "FinalCandidateAction", "ModelTurn", "TextAction", "ToolCallAction"]
