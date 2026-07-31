"""Application composition for independently owned hosted-service families."""

from __future__ import annotations

import inspect

from mote.contracts.ports.service.endpoint import ServiceEndpointAdapter, ServiceEndpointResolver
from mote.contracts.service import ServiceEndpointDescriptor


class ProductServiceEndpointResolver:
    """Resolve an endpoint through the ordered Product adapter families."""

    def __init__(self, *resolvers: ServiceEndpointResolver) -> None:
        self._resolvers = resolvers

    def resolve(
        self,
        endpoint: ServiceEndpointDescriptor,
        credential_slot_id: str,
    ) -> ServiceEndpointAdapter | None:
        for resolver in self._resolvers:
            adapter = resolver.resolve(endpoint, credential_slot_id)
            if adapter is not None:
                return adapter
        return None

    async def aclose(self) -> None:
        for resolver in reversed(self._resolvers):
            close = getattr(resolver, "aclose", None)
            if close is None:
                continue
            result = close()
            if inspect.isawaitable(result):
                await result


__all__ = ["ProductServiceEndpointResolver"]
