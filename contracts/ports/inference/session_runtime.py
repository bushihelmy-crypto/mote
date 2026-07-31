from typing import AsyncIterator, Protocol

from mote.contracts.inference.events import SessionLifecycleEvent
from mote.contracts.inference.executions import BoundExecutionRequest, SessionApplicationMessage
from mote.contracts.inference.wire_permit import WirePermit


class SessionExecution(Protocol):
    def __aiter__(self) -> AsyncIterator[SessionLifecycleEvent]:
        ...

    async def authorize_open(self, permit: WirePermit) -> None:
        ...

    async def send(self, message: SessionApplicationMessage, permit: WirePermit) -> None:
        ...

    async def close(self, reason: str) -> None:
        ...


class SessionRuntime(Protocol):
    async def open(self, request: BoundExecutionRequest) -> SessionExecution:
        ...

    async def drain(self, *, timeout_seconds: float) -> None:
        ...

    async def aclose(self) -> None:
        ...
