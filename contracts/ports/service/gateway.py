"""Runtime execution port for externally hosted Tool capabilities."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from mote.contracts.service import ResolvedServiceResponse, ServiceInvocation


@runtime_checkable
class ServiceGateway(Protocol):
    def supports_route(self, route_id: str, capability: str) -> bool: ...

    async def execute(
        self,
        invocation: ServiceInvocation,
    ) -> ResolvedServiceResponse: ...

    async def resume(
        self,
        invocation: ServiceInvocation,
    ) -> ResolvedServiceResponse: ...

    async def reconcile(self, invocation: ServiceInvocation) -> ResolvedServiceResponse: ...

    async def cancel(self, service_call_id: str) -> bool: ...

    async def aclose(self) -> None: ...


__all__ = ["ServiceGateway"]
