"""Strongly typed client seam for Shared daemon runtime adapters."""

from collections.abc import AsyncIterator
from typing import Protocol

from mote.contracts.inference.shared import ProtocolNegotiationResult, SharedHandshake
from mote.contracts.inference.wire_permit import WirePermit
from mote.product.inference.daemon.messages import (
    DaemonReadinessView,
    ExecutionReceiptView,
    GenerationStatusView,
    LifecycleEventView,
    SessionMessageCommand,
    StartExecutionCommand,
    StartExecutionReceipt,
    TransferExecutionCommand,
)
from mote.product.inference.security.permit_issuer import ProductWirePermitIssuer


class SharedRuntimeClient(Protocol):
    def permit_issuer(self) -> ProductWirePermitIssuer: ...

    async def stage_generation(
        self, artifact: bytes, *, generation_id: str, artifact_digest: str
    ) -> GenerationStatusView: ...

    async def get_readiness(self, *, timeout: float | None = None) -> DaemonReadinessView: ...

    async def start_unary(
        self, request: StartExecutionCommand, *, timeout: float | None = None
    ) -> StartExecutionReceipt: ...

    async def start_durable_command(
        self, request: StartExecutionCommand, *, timeout: float | None = None
    ) -> StartExecutionReceipt: ...

    async def open_session(
        self, request: StartExecutionCommand, *, timeout: float | None = None
    ) -> StartExecutionReceipt: ...

    async def execute_transfer_part(
        self, request: TransferExecutionCommand, *, timeout: float | None = None
    ) -> StartExecutionReceipt: ...

    async def authorize_wire(
        self,
        execution_id: str,
        permit: WirePermit,
        *,
        generation_id: str,
        timeout: float | None = None,
    ) -> None: ...

    async def cancel(
        self,
        execution_id: str,
        reason: str,
        *,
        generation_id: str,
        timeout: float | None = None,
    ) -> None: ...

    def session(self, requests: AsyncIterator[SessionMessageCommand]) -> AsyncIterator[LifecycleEventView]: ...

    async def query_receipt(
        self,
        execution_id: str,
        *,
        generation_id: str,
        timeout: float | None = None,
    ) -> ExecutionReceiptView: ...

    def resume_events(
        self,
        execution_id: str,
        *,
        generation_id: str,
        after_sequence: int,
        receipt_revision: int,
        timeout: float | None = None,
    ) -> AsyncIterator[LifecycleEventView]: ...

    async def close(self) -> None: ...


class AuthenticatingSharedRuntimeClient(SharedRuntimeClient, Protocol):
    async def authenticate(self, handshake: SharedHandshake) -> ProtocolNegotiationResult: ...


__all__ = ["AuthenticatingSharedRuntimeClient", "SharedRuntimeClient"]
