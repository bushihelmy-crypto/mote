"""Product adapter seam for exactly one provider wire request."""

from __future__ import annotations

from typing import Protocol

from mote.contracts.artifacts import ResolvedArtifact
from mote.contracts.models.failover import EndpointDescriptor, FailureDisposition
from mote.contracts.models.invocation import CanonicalModelResponse, ModelInvocation


class ModelEndpointAdapter(Protocol):
    endpoint_id: str
    credential_slot_id: str
    tenant_fingerprint: str

    async def execute_once(
        self,
        invocation: ModelInvocation,
        endpoint: EndpointDescriptor,
        *,
        timeout_seconds: float,
        stream: bool = False,
        artifact: ResolvedArtifact | None = None,
    ) -> CanonicalModelResponse:
        ...

    def classify(self, exc: Exception) -> FailureDisposition:
        ...

    async def aclose(self) -> None:
        ...


class ModelEndpointResolver(Protocol):
    def resolve(
        self,
        endpoint: EndpointDescriptor,
        credential_slot_id: str,
    ) -> ModelEndpointAdapter | None:
        ...


__all__ = ["ModelEndpointAdapter", "ModelEndpointResolver"]
