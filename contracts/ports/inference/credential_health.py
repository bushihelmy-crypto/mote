from typing import Protocol

from mote.contracts.inference.governance import CredentialHealthObservation


class CredentialHealthAuthorityPort(Protocol):
    async def allow(self, credential_slot_id: str, credential_version: str) -> bool:
        ...

    async def observe(self, observation: CredentialHealthObservation) -> None:
        ...
