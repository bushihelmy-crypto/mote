"""Request-local canonical model-request transformation capability."""

from __future__ import annotations

from typing import Protocol

from mote.contracts.model.failover import EndpointDescriptor, FailureDisposition, RequestTransform
from mote.contracts.model.invocation import ModelInvocation


class ModelRequestTransformer(Protocol):
    async def transform(
        self,
        invocation: ModelInvocation,
        transform: RequestTransform,
        disposition: FailureDisposition,
        endpoint: EndpointDescriptor,
    ) -> ModelInvocation | None:
        """Return a changed invocation, or ``None`` when no progress is possible."""
        ...


__all__ = ["ModelRequestTransformer"]
