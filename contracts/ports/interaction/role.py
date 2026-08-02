"""Turn-scoped human interaction capability consumed by an Agent."""

from __future__ import annotations

from typing import Protocol

from mote.contracts.interaction import ApprovalChoice, ApprovalRequest, AskUserQuestionAnswers, AskUserQuestionInput
from mote.contracts.interaction.handoff import DriverHandoffHandle, HandoffRequest, HumanHandoffOutcome
from mote.contracts.surface import LiveSurfaceSession


class RoleHumanInteractionPort(Protocol):
    async def ask_user(self, question: str, *, sent_from: str) -> str: ...

    async def ask_user_question(self, questions: AskUserQuestionInput, *, sent_from: str) -> AskUserQuestionAnswers: ...

    async def request_approval(self, request: ApprovalRequest, *, sent_from: str) -> ApprovalChoice: ...

    async def reply_to_user(self, content: str, *, sent_from: str) -> str: ...

    async def open_handoff(
        self,
        request: HandoffRequest,
        handle: DriverHandoffHandle,
        surface: LiveSurfaceSession | None = None,
    ) -> HumanHandoffOutcome: ...


__all__ = ["RoleHumanInteractionPort"]
