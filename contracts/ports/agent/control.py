"""Minimal typed control-plane capability consumed by child spawn sites."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from types import TracebackType
from typing import Protocol, Self, TypeVar

from mote.contracts.agent.capacity import ResidentCapacitySettlementReceipt
from mote.contracts.agent.spawn import RunnableAgent, SpawnPlan
from mote.contracts.conversation import Message
from mote.contracts.output import RunOutcome
from mote.contracts.ports.agent.team_roster import TeamRosterProvider

OutputT = TypeVar("OutputT")


class ChildAgentHandlePort(Protocol[OutputT]):
    @property
    def agent(self) -> RunnableAgent[OutputT]: ...

    async def run_to_completion(self, message: Message) -> RunOutcome[OutputT] | None: ...

    async def aclose(self) -> None: ...

    async def __aenter__(self) -> Self: ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool: ...


class AgentControlPort(TeamRosterProvider, Protocol):
    async def spawn_agent(self, spec: SpawnPlan[OutputT]) -> ChildAgentHandlePort[OutputT]: ...


class ChildReleaseDisposition(StrEnum):
    SETTLED = "settled"
    ALREADY_TERMINAL = "already_terminal"
    CLEANUP_FAILED = "cleanup_failed"
    OWNER_LOST = "owner_lost"


@dataclass(frozen=True, slots=True)
class ChildReleaseReceipt:
    agent_id: str
    disposition: ChildReleaseDisposition
    lifecycle_revision: int
    detail: str = ""


class ChildReleaseError(RuntimeError):
    """A child handle could not reach a terminal release disposition."""

    def __init__(self, receipt: ChildReleaseReceipt) -> None:
        self.receipt = receipt
        super().__init__(
            f"child {receipt.agent_id!r} release remains " f"{receipt.disposition.value}: {receipt.detail}"
        )


class ChildReleasePort(Protocol):
    async def release_child(self, agent_id: str) -> ChildReleaseReceipt: ...


class ResidencyReservationPort(Protocol):
    def rollback(self) -> ResidentCapacitySettlementReceipt: ...


__all__ = [
    "AgentControlPort",
    "ChildAgentHandlePort",
    "ChildReleaseDisposition",
    "ChildReleaseError",
    "ChildReleaseReceipt",
    "ChildReleasePort",
    "ResidencyReservationPort",
]
