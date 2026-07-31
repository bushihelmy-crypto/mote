import asyncio

from mote.contracts.inference.attempt import InferenceAttemptRequest
from mote.contracts.inference.executions import BoundExecutionRequest
from mote.contracts.ports.inference.provider_transport import GenerateTransport
from mote.contracts.ports.inference.session_transport import SessionTransport


class ProductGenerateTransportResolver:
    def __init__(self, transports: dict[tuple[str, str], GenerateTransport]) -> None:
        self._transports = dict(transports)

    def resolve_generate(self, request: InferenceAttemptRequest) -> GenerateTransport:
        key = (request.endpoint.transport, request.credential_slot_id)
        try:
            return self._transports[key]
        except KeyError as exc:
            raise LookupError(f"no generate transport for protocol {key[0]!r} and credential slot") from exc

    async def aclose(self) -> None:
        unique = {id(transport): transport for transport in self._transports.values()}
        await asyncio.gather(
            *(transport.aclose() for transport in unique.values()),
            return_exceptions=True,
        )


class ProductSessionTransportResolver:
    def __init__(self, transports: dict[tuple[str, str], SessionTransport]) -> None:
        self._transports = dict(transports)

    def resolve_session(self, request: BoundExecutionRequest) -> SessionTransport:
        key = (request.endpoint_binding_id, request.credential_slot_id)
        try:
            return self._transports[key]
        except KeyError as exc:
            raise LookupError("no session transport for endpoint and credential slot") from exc

    async def aclose(self) -> None:
        unique = {id(transport): transport for transport in self._transports.values()}
        await asyncio.gather(
            *(transport.aclose() for transport in unique.values()),
            return_exceptions=True,
        )
