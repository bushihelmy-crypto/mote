from typing import Protocol

from mote.contracts.inference.governance import ProviderQuotaObservation


class ProviderQuotaAuthorityPort(Protocol):
    async def allow(self, provider: str, endpoint_id: str, credential_slot_id: str, *, estimated_tokens: int) -> bool:
        ...

    async def observe(self, observation: ProviderQuotaObservation) -> None:
        ...
