from typing import AsyncIterator, Protocol

from mote.contracts.inference.executions import BoundExecutionRequest, SessionApplicationMessage
from mote.contracts.inference.transport import ProviderWireResult
from mote.contracts.ports.inference.provider_transport import WireLifecycleSink


class ProviderSessionConnection(Protocol):
    async def send_once(
        self,
        message: SessionApplicationMessage,
        *,
        local_deadline: float,
        lifecycle: WireLifecycleSink,
    ) -> ProviderWireResult:
        ...

    def inbound(self) -> AsyncIterator[dict]:
        ...

    async def close(self, reason: str) -> None:
        ...


class SessionOpenResult(Protocol):
    connection: ProviderSessionConnection
    wire_result: ProviderWireResult


class SessionTransport(Protocol):
    provider: str
    endpoint_id: str
    wire_protocol: str

    async def open_once(
        self,
        request: BoundExecutionRequest,
        *,
        local_deadline: float,
        lifecycle: WireLifecycleSink,
    ) -> SessionOpenResult:
        ...

    async def aclose(self) -> None:
        ...


class SessionTransportResolver(Protocol):
    def resolve_session(self, request: BoundExecutionRequest) -> SessionTransport:
        ...
