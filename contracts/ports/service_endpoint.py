"""Single-wire Product seams for externally hosted Tool capabilities."""

from __future__ import annotations

from typing import Protocol

from mote.contracts.services import (
    ServiceEndpointDescriptor,
    ServiceEndpointFailure,
    ServiceEndpointOutcome,
    ServiceInvocation,
    ServiceReceipt,
)


class ServiceEndpointAdapter(Protocol):
    endpoint_id: str
    credential_slot_id: str
    tenant_fingerprint: str

    async def start_once(
        self,
        invocation: ServiceInvocation,
        endpoint: ServiceEndpointDescriptor,
        *,
        timeout_seconds: float,
    ) -> ServiceEndpointOutcome:
        ...

    async def poll_once(
        self,
        receipt: ServiceReceipt,
        endpoint: ServiceEndpointDescriptor,
        *,
        timeout_seconds: float,
    ) -> ServiceEndpointOutcome:
        ...

    async def reconcile_once(
        self,
        invocation: ServiceInvocation,
        endpoint: ServiceEndpointDescriptor,
        *,
        timeout_seconds: float,
    ) -> ServiceEndpointOutcome | None:
        ...

    async def cancel_once(
        self,
        receipt: ServiceReceipt,
        endpoint: ServiceEndpointDescriptor,
        *,
        timeout_seconds: float,
    ) -> None:
        ...

    def classify_start(self, exc: Exception) -> ServiceEndpointFailure:
        ...

    def classify_poll(self, exc: Exception) -> ServiceEndpointFailure:
        ...

    async def aclose(self) -> None:
        ...


class ServiceEndpointResolver(Protocol):
    def resolve(
        self,
        endpoint: ServiceEndpointDescriptor,
        credential_slot_id: str,
    ) -> ServiceEndpointAdapter | None:
        ...


__all__ = ["ServiceEndpointAdapter", "ServiceEndpointResolver"]
