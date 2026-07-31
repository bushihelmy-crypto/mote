from typing import Any, Protocol

from mote.contracts.inference.attempt import InferenceAttemptRequest
from mote.contracts.inference.transport import ProviderWireResult


class WireLifecycleSink(Protocol):
    async def wire_started(self) -> None:
        ...

    async def response_started(self) -> None:
        ...


class StreamSink(Protocol):
    async def emit(self, chunk: dict[str, Any]) -> None:
        ...


class GenerateTransport(Protocol):
    async def generate_once(
        self,
        request: InferenceAttemptRequest,
        *,
        local_deadline: float,
        lifecycle: WireLifecycleSink,
        stream: StreamSink | None,
    ) -> ProviderWireResult:
        ...

    async def aclose(self) -> None:
        ...


class GenerateTransportResolver(Protocol):
    def resolve_generate(self, request: InferenceAttemptRequest) -> GenerateTransport:
        ...
