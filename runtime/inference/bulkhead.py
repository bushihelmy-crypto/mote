"""Atomic hierarchical in-flight capacity and provider isolation."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class BulkheadIdentity:
    provider: str
    endpoint: str
    wire_protocol: str

    def __post_init__(self) -> None:
        if not self.provider or not self.endpoint or not self.wire_protocol:
            raise ValueError("bulkhead identity fields are required")


class BulkheadPermit:
    def __init__(self, controller: "BulkheadController", identity: BulkheadIdentity) -> None:
        self._controller = controller
        self.identity = identity
        self._released = False

    async def release(self) -> None:
        if self._released:
            raise RuntimeError("bulkhead permit already released")
        self._released = True
        await self._controller._release(self.identity)

    async def __aenter__(self) -> "BulkheadPermit":
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        await self.release()


class BulkheadController:
    """One atomic authority for global, provider and endpoint wire capacity."""

    def __init__(
        self,
        *,
        global_limit: int,
        provider_limit: int,
        endpoint_limit: int,
    ) -> None:
        if min(global_limit, provider_limit, endpoint_limit) <= 0:
            raise ValueError("bulkhead limits must be positive")
        if endpoint_limit > provider_limit or provider_limit > global_limit:
            raise ValueError("bulkhead limits must be nested endpoint <= provider <= global")
        self._global_limit = global_limit
        self._provider_limit = provider_limit
        self._endpoint_limit = endpoint_limit
        self._condition = asyncio.Condition()
        self._global_in_flight = 0
        self._provider_in_flight: dict[str, int] = {}
        self._endpoint_in_flight: dict[BulkheadIdentity, int] = {}
        self._closed = False

    @property
    def global_in_flight(self) -> int:
        return self._global_in_flight

    def in_flight(self, identity: BulkheadIdentity) -> int:
        return self._endpoint_in_flight.get(identity, 0)

    async def acquire(self, identity: BulkheadIdentity, *, deadline: float) -> BulkheadPermit:
        async with self._condition:
            loop = asyncio.get_running_loop()
            while not self._available(identity):
                if self._closed:
                    raise RuntimeError("bulkhead controller is closed")
                remaining = deadline - loop.time()
                if remaining <= 0:
                    raise TimeoutError("bulkhead deadline exceeded")
                await asyncio.wait_for(self._condition.wait(), timeout=remaining)
            if self._closed:
                raise RuntimeError("bulkhead controller is closed")
            self._global_in_flight += 1
            self._provider_in_flight[identity.provider] = self._provider_in_flight.get(identity.provider, 0) + 1
            self._endpoint_in_flight[identity] = self._endpoint_in_flight.get(identity, 0) + 1
            return BulkheadPermit(self, identity)

    async def close(self) -> None:
        async with self._condition:
            self._closed = True
            self._condition.notify_all()

    async def _release(self, identity: BulkheadIdentity) -> None:
        async with self._condition:
            provider_count = self._provider_in_flight.get(identity.provider, 0)
            endpoint_count = self._endpoint_in_flight.get(identity, 0)
            if self._global_in_flight <= 0 or provider_count <= 0 or endpoint_count <= 0:
                raise RuntimeError("bulkhead capacity underflow")
            self._global_in_flight -= 1
            self._decrement(self._provider_in_flight, identity.provider)
            self._decrement(self._endpoint_in_flight, identity)
            self._condition.notify_all()

    def _available(self, identity: BulkheadIdentity) -> bool:
        return (
            self._global_in_flight < self._global_limit
            and self._provider_in_flight.get(identity.provider, 0) < self._provider_limit
            and self._endpoint_in_flight.get(identity, 0) < self._endpoint_limit
        )

    @staticmethod
    def _decrement(counts, key) -> None:
        value = counts[key] - 1
        if value:
            counts[key] = value
        else:
            del counts[key]
