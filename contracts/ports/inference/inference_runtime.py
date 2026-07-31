from typing import AsyncIterator, Protocol

from mote.contracts.inference.attempt import InferenceAttemptRequest
from mote.contracts.inference.events import AttemptLifecycleEvent
from mote.contracts.inference.wire_permit import WirePermit


class AttemptExecution(Protocol):
    def __aiter__(self) -> AsyncIterator[AttemptLifecycleEvent]:
        ...

    async def authorize_wire(self, permit: WirePermit) -> None:
        ...

    async def cancel(self, reason: str) -> None:
        ...


class InferenceRuntime(Protocol):
    async def start_attempt(self, request: InferenceAttemptRequest) -> AttemptExecution:
        ...

    async def drain(self, *, timeout_seconds: float) -> None:
        ...

    async def aclose(self) -> None:
        ...
