from typing import Protocol

from mote.contracts.inference.executions import BoundExecutionRequest
from mote.contracts.inference.transport import ProviderWireResult
from mote.contracts.ports.inference.provider_transport import WireLifecycleSink


class BoundCommandTransport(Protocol):
    provider: str
    endpoint_id: str
    wire_protocol: str

    async def execute_once(
        self,
        request: BoundExecutionRequest,
        *,
        local_deadline: float,
        lifecycle: WireLifecycleSink,
    ) -> ProviderWireResult:
        ...

    async def aclose(self) -> None:
        ...


class BoundCommandTransportResolver(Protocol):
    def resolve_command(self, request: BoundExecutionRequest) -> BoundCommandTransport:
        ...
