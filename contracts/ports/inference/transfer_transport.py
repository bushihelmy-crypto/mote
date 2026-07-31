from typing import Protocol

from mote.contracts.inference.executions import TransferPartRequest
from mote.contracts.inference.transport import ProviderWireResult
from mote.contracts.ports.inference.provider_transport import WireLifecycleSink


class ProviderTransferPartTransport(Protocol):
    provider: str
    endpoint_id: str
    wire_protocol: str

    async def execute_once(
        self,
        request: TransferPartRequest,
        *,
        local_deadline: float,
        lifecycle: WireLifecycleSink,
    ) -> ProviderWireResult:
        ...

    async def aclose(self) -> None:
        ...


class ProviderTransferPartTransportResolver(Protocol):
    def resolve_transfer_part(self, request: TransferPartRequest) -> ProviderTransferPartTransport:
        ...
