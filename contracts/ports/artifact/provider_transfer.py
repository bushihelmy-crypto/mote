from typing import AsyncIterator, Protocol

from mote.contracts.inference.events import AttemptLifecycleEvent
from mote.contracts.inference.executions import TransferPartRequest
from mote.contracts.inference.wire_permit import WirePermit


class TransferPartExecution(Protocol):
    def __aiter__(self) -> AsyncIterator[AttemptLifecycleEvent]:
        ...

    async def authorize_wire(self, permit: WirePermit) -> None:
        ...

    async def cancel(self, reason: str) -> None:
        ...


class ProviderArtifactTransferRuntime(Protocol):
    async def execute_part(self, request: TransferPartRequest) -> TransferPartExecution:
        ...

    async def drain(self, *, timeout_seconds: float) -> None:
        ...

    async def aclose(self) -> None:
        ...
