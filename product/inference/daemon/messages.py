"""Typed internal messages at the Shared gRPC adapter boundary."""

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Protocol, TypeAlias

from mote.contracts.inference.events import AttemptLifecycleEvent, SessionLifecycleEvent
from mote.contracts.inference.executions import SessionApplicationMessage
from mote.contracts.inference.wire_permit import WirePermit

LifecycleEvent: TypeAlias = AttemptLifecycleEvent | SessionLifecycleEvent


@dataclass(frozen=True, slots=True)
class RpcEnvelopeBinding:
    generation_id: str
    generation_artifact_digest: str
    deadline_utc: str = ""
    remaining_seconds_at_send: float = 0.0
    sent_at_utc: str = ""
    traceparent: str = ""
    idempotency_key: str = ""


@dataclass(frozen=True, slots=True)
class StartExecutionCommand:
    envelope: RpcEnvelopeBinding
    execution_id: str
    operation: str
    canonical_request: bytes
    artifact_reference: str = ""


@dataclass(frozen=True, slots=True)
class TransferExecutionCommand:
    start: StartExecutionCommand
    part_number: int
    offset: int
    length: int
    content_digest: str


@dataclass(frozen=True, slots=True)
class ExecutionQuery:
    envelope: RpcEnvelopeBinding
    execution_id: str


@dataclass(frozen=True, slots=True)
class AuthorizeExecutionCommand(ExecutionQuery):
    permit: WirePermit


@dataclass(frozen=True, slots=True)
class CancelExecutionCommand(ExecutionQuery):
    reason: str


@dataclass(frozen=True, slots=True)
class EventCursor(ExecutionQuery):
    after_sequence: int
    receipt_revision: int


@dataclass(frozen=True, slots=True)
class SessionMessageCommand(ExecutionQuery):
    application_sequence: int
    message: SessionApplicationMessage
    permit: WirePermit


@dataclass(frozen=True, slots=True)
class ExecutionReceiptView:
    execution_id: str
    revision: int
    state: str
    terminal_artifact_reference: str = ""


@dataclass(frozen=True, slots=True)
class StartExecutionReceipt:
    execution_id: str
    receipt_revision: int


@dataclass(frozen=True, slots=True)
class GenerationStatusView:
    generation_id: str
    artifact_digest: str
    state: str


@dataclass(frozen=True, slots=True)
class DaemonReadinessView:
    ready: bool
    components: tuple[tuple[str, str], ...]

    def component(self, identity: str) -> str:
        values = dict(self.components)
        try:
            return values[identity]
        except KeyError as exc:
            raise RuntimeError(f"daemon readiness component {identity!r} is missing") from exc


@dataclass(frozen=True, slots=True)
class LifecycleEventView:
    execution_id: str
    sequence: int
    receipt_revision: int
    event_type: str
    payload: bytes


@dataclass(frozen=True, slots=True)
class GenerationCommand:
    envelope: RpcEnvelopeBinding
    generation_artifact: bytes = b""


class FiniteExecution(Protocol):
    def __aiter__(self) -> AsyncIterator[AttemptLifecycleEvent]: ...
    async def authorize_wire(self, permit: WirePermit) -> None: ...
    async def cancel(self, reason: str) -> None: ...


class SessionExecution(Protocol):
    def __aiter__(self) -> AsyncIterator[SessionLifecycleEvent]: ...
    async def authorize_open(self, permit: WirePermit) -> None: ...
    async def send(self, message: SessionApplicationMessage, permit: WirePermit) -> None: ...
    async def close(self, reason: str) -> None: ...


__all__ = [
    "AuthorizeExecutionCommand",
    "CancelExecutionCommand",
    "EventCursor",
    "ExecutionQuery",
    "ExecutionReceiptView",
    "FiniteExecution",
    "GenerationCommand",
    "DaemonReadinessView",
    "GenerationStatusView",
    "LifecycleEvent",
    "LifecycleEventView",
    "RpcEnvelopeBinding",
    "SessionExecution",
    "SessionMessageCommand",
    "StartExecutionCommand",
    "TransferExecutionCommand",
    "StartExecutionReceipt",
]
