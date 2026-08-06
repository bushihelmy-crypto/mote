"""Kernel-facing persistence boundary for accepting one Act frontier."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from mote.contracts.conversation import Message
from mote.contracts.events.envelope import JsonValue
from mote.contracts.events.pending_act import PendingActionResultCommittedEvent, PendingActionsSkippedEvent
from mote.contracts.execution.pending_act import PendingActFrontier
from mote.contracts.execution.pending_act_claim import PendingActInvokePermit
from mote.contracts.interaction.approval_identity import ApprovalRequestId
from mote.contracts.ports.tool.approval import ToolApprovalIntent, ToolApprovalResolution
from mote.contracts.tool.actions import ToolCallAction
from mote.contracts.tool.catalog import ToolBindingSnapshot
from mote.contracts.tool.external_effect import ToolEffectReceipt
from mote.contracts.tool.identity import ToolInvocationIdentity


@dataclass(frozen=True, slots=True)
class PendingActAcceptance:
    frontier: PendingActFrontier


@dataclass(frozen=True, slots=True)
class PendingActResume:
    frontier: PendingActFrontier
    actions: tuple[ToolCallAction, ...]
    completed_invocation_ids: frozenset[str] = frozenset()
    skipped_invocation_ids: frozenset[str] = frozenset()
    committed_result_messages: tuple[Message, ...] = ()


@dataclass(frozen=True, slots=True)
class ExternalEffectPermit:
    frontier: PendingActFrontier
    identity: ToolInvocationIdentity


class PendingActAcceptancePort(Protocol):
    async def accept(
        self,
        actions: tuple[ToolCallAction, ...],
        snapshot: ToolBindingSnapshot,
        messages: tuple[Message, ...],
    ) -> PendingActAcceptance: ...

    async def settle(
        self,
        acceptance: PendingActAcceptance,
        messages: tuple[Message, ...],
        *,
        continue_inference: bool,
        effect_receipts: tuple[ToolEffectReceipt, ...] = (),
        action_results: tuple[PendingActionResultCommittedEvent, ...] = (),
        skipped: PendingActionsSkippedEvent | None = None,
        rejected_approval_request_id: ApprovalRequestId | None = None,
    ) -> None: ...

    def resume(self, frontier: PendingActFrontier, snapshot: ToolBindingSnapshot) -> PendingActResume: ...

    async def begin_external_effect(
        self,
        acceptance: PendingActAcceptance,
        ordinal: int,
        identity: ToolInvocationIdentity,
    ) -> ExternalEffectPermit: ...

    async def begin_invoke(
        self,
        acceptance: PendingActAcceptance,
        ordinal: int,
        identity: ToolInvocationIdentity,
    ) -> PendingActInvokePermit: ...

    async def mark_external_effect_in_doubt(
        self,
        permit: ExternalEffectPermit,
        *,
        evidence: JsonValue,
    ) -> None: ...

    async def resolve_approval(
        self, acceptance: PendingActAcceptance, intent: ToolApprovalIntent
    ) -> ToolApprovalResolution: ...


__all__ = [
    "ExternalEffectPermit",
    "PendingActAcceptance",
    "PendingActAcceptancePort",
    "PendingActResume",
]
