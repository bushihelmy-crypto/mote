"""Revision-consistent execution restore outcomes."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Generic, Protocol, TypeAlias, TypeVar

from mote.contracts.conversation import Message
from mote.contracts.execution.pending_act import PendingActFrontier
from mote.contracts.execution.run_cursor import RunRecoveryCursor
from mote.contracts.output import CommittedOutput
from mote.contracts.tool.identity import ToolInvocationId

OutputT = TypeVar("OutputT")


@dataclass(frozen=True, slots=True)
class CommittedExecution(Generic[OutputT]):
    result: CommittedOutput[OutputT]
    presentation: Message


@dataclass(frozen=True, slots=True)
class PendingActExecution:
    frontier: PendingActFrontier
    cursor: RunRecoveryCursor


@dataclass(frozen=True, slots=True)
class ObserveExecution:
    cursor: RunRecoveryCursor


@dataclass(frozen=True, slots=True)
class NoPendingExecution:
    pass


@dataclass(frozen=True, slots=True)
class InDoubtExecution:
    frontier: PendingActFrontier
    invocation_ids: tuple[ToolInvocationId, ...]


@dataclass(frozen=True, slots=True)
class ExternalEffectReconciliationRequired:
    """A STARTED effect may only be queried or recovered idempotently."""

    frontier: PendingActFrontier
    invocation_ids: tuple[ToolInvocationId, ...]


@dataclass(frozen=True, slots=True)
class InterruptedExecution:
    run_id: str


@dataclass(frozen=True, slots=True)
class InterruptedExecutionNeedsSettlement:
    run_id: str


@dataclass(frozen=True, slots=True)
class UnrecoverablePreV1Execution:
    run_id: str
    code: str = "UNRECOVERABLE_PRE_V1_PENDING_ACT"


ExecutionRestore: TypeAlias = (
    CommittedExecution[OutputT]
    | PendingActExecution
    | ExternalEffectReconciliationRequired
    | InDoubtExecution
    | InterruptedExecution
    | InterruptedExecutionNeedsSettlement
    | UnrecoverablePreV1Execution
    | ObserveExecution
    | NoPendingExecution
)


class ExecutionRestorePort(Protocol[OutputT]):
    def snapshot(self) -> ExecutionRestore[OutputT]: ...


__all__ = [
    "ExecutionRestore",
    "ExecutionRestorePort",
    "CommittedExecution",
    "ExternalEffectReconciliationRequired",
    "InDoubtExecution",
    "InterruptedExecution",
    "InterruptedExecutionNeedsSettlement",
    "NoPendingExecution",
    "ObserveExecution",
    "PendingActExecution",
    "UnrecoverablePreV1Execution",
]
