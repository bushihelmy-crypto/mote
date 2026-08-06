"""Narrow durable approval boundary consumed by tool execution."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from mote.contracts.interaction.approval_identity import ApprovalRequestId
from mote.contracts.tool.arguments import ToolArguments
from mote.contracts.tool.identity import ToolInvocationIdentity


@dataclass(frozen=True, slots=True)
class ToolApprovalIntent:
    identity: ToolInvocationIdentity
    tool_name: str
    arguments: ToolArguments
    permission_targets: tuple[str, ...]
    mutates_fs: bool
    reason: str


@dataclass(frozen=True, slots=True)
class ToolApprovalResolution:
    approved: bool
    allow_session: bool = False
    request_id: ApprovalRequestId | None = None
    revised_arguments: ToolArguments | None = None


class ToolApprovalCoordinator(Protocol):
    async def resolve(self, intent: ToolApprovalIntent) -> ToolApprovalResolution: ...


__all__ = ["ToolApprovalCoordinator", "ToolApprovalIntent", "ToolApprovalResolution"]
