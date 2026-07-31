"""Embedded one-part artifact transfer data plane."""

from __future__ import annotations

from typing import cast

from mote.contracts.inference.executions import BoundExecutionRequest, TransferPartRequest
from mote.contracts.inference.wire_permit import ExecutionTaxonomy
from mote.contracts.ports.inference.command_transport import BoundCommandTransport
from mote.contracts.ports.inference.transfer_transport import ProviderTransferPartTransportResolver
from mote.runtime.inference.command_runtime import EmbeddedServiceCommandRuntime, _CommandExecution
from mote.runtime.inference.generation import GenerationDomain


class EmbeddedArtifactTransferRuntime(EmbeddedServiceCommandRuntime):
    _generation_domain = GenerationDomain.TRANSFER
    _execution_taxonomy = ExecutionTaxonomy.ARTIFACT_TRANSFER
    _reservation_namespace = "transfer-part"
    _idempotency_class = "artifact_transfer_part"

    async def execute_part(self, request: TransferPartRequest) -> _CommandExecution:
        return await self._start(request)

    def _resolve_transport(self, request: BoundExecutionRequest) -> BoundCommandTransport:
        if not isinstance(request, TransferPartRequest):
            raise TypeError("artifact transfer runtime requires TransferPartRequest")
        resolver = cast(ProviderTransferPartTransportResolver, self._transports)
        return cast(BoundCommandTransport, resolver.resolve_transfer_part(request))
