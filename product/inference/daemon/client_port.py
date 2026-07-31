"""Strongly typed client seam for Shared daemon runtime adapters."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Protocol, TypeVar

from mote.contracts.inference.shared import ProtocolNegotiationResult, SharedHandshake
from mote.product.inference.security.permit_issuer import ProductWirePermitIssuer


class RpcEnvelope(Protocol):
    ...


class RpcStartResponse(Protocol):
    execution_id: str
    receipt_revision: int


class RpcLifecycleEvent(Protocol):
    execution_id: str
    sequence: int
    receipt_revision: int
    event_type: str
    payload: bytes


class RpcReceipt(Protocol):
    execution_id: str
    revision: int
    state: str


class RpcGenerationStatus(Protocol):
    generation_id: str
    artifact_digest: str
    state: str


StartRequestT = TypeVar("StartRequestT", contravariant=True)
TransferRequestT = TypeVar("TransferRequestT", contravariant=True)
SessionMessageT = TypeVar("SessionMessageT", contravariant=True)


class SharedRuntimeClient(Protocol[StartRequestT, TransferRequestT, SessionMessageT]):
    def envelope(self, **fields: object) -> RpcEnvelope:
        ...

    def permit_issuer(self) -> ProductWirePermitIssuer:
        ...

    async def stage_generation(
        self, artifact: bytes, *, generation_id: str, artifact_digest: str
    ) -> RpcGenerationStatus:
        ...

    async def start_unary(self, request: StartRequestT, *, timeout: float | None = None) -> RpcStartResponse:
        ...

    async def start_durable_command(self, request: StartRequestT, *, timeout: float | None = None) -> RpcStartResponse:
        ...

    async def open_session(self, request: StartRequestT, *, timeout: float | None = None) -> RpcStartResponse:
        ...

    async def execute_transfer_part(
        self, request: TransferRequestT, *, timeout: float | None = None
    ) -> RpcStartResponse:
        ...

    async def authorize_wire(
        self,
        execution_id: str,
        wire_permit: bytes,
        *,
        generation_id: str,
        timeout: float | None = None,
    ) -> None:
        ...

    async def cancel(
        self,
        execution_id: str,
        reason: str,
        *,
        generation_id: str,
        timeout: float | None = None,
    ) -> None:
        ...

    def session(self, requests: AsyncIterator[SessionMessageT]) -> AsyncIterator[RpcLifecycleEvent]:
        ...

    async def query_receipt(
        self,
        execution_id: str,
        *,
        generation_id: str,
        timeout: float | None = None,
    ) -> RpcReceipt:
        ...

    def resume_events(
        self,
        execution_id: str,
        *,
        generation_id: str,
        after_sequence: int,
        receipt_revision: int,
        timeout: float | None = None,
    ) -> AsyncIterator[RpcLifecycleEvent]:
        ...

    async def close(self) -> None:
        ...


class AuthenticatingSharedRuntimeClient(
    SharedRuntimeClient[StartRequestT, TransferRequestT, SessionMessageT],
    Protocol[StartRequestT, TransferRequestT, SessionMessageT],
):
    async def authenticate(self, handshake: SharedHandshake) -> ProtocolNegotiationResult:
        ...


__all__ = [
    "AuthenticatingSharedRuntimeClient",
    "RpcEnvelope",
    "RpcGenerationStatus",
    "RpcLifecycleEvent",
    "RpcReceipt",
    "RpcStartResponse",
    "SharedRuntimeClient",
]
