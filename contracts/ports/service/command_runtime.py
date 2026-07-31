from typing import AsyncIterator, Protocol

from mote.contracts.inference.events import AttemptLifecycleEvent
from mote.contracts.inference.executions import BoundExecutionRequest
from mote.contracts.inference.wire_permit import WirePermit


class CommandExecution(Protocol):
    def __aiter__(self) -> AsyncIterator[AttemptLifecycleEvent]:
        ...

    async def authorize_wire(self, permit: WirePermit) -> None:
        ...

    async def cancel(self, reason: str) -> None:
        ...


class ServiceCommandRuntime(Protocol):
    async def start_command(self, request: BoundExecutionRequest) -> CommandExecution:
        ...

    async def drain(self, *, timeout_seconds: float) -> None:
        ...

    async def aclose(self) -> None:
        ...
