"""Product-owned credential capabilities with explicit redaction and lifetime."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class SecretIdentity:
    value: str

    def __repr__(self) -> str:
        return "SecretIdentity(<redacted>)"


@dataclass(frozen=True, slots=True)
class CredentialEpoch:
    value: str

    def __repr__(self) -> str:
        return "CredentialEpoch(<redacted>)"


class CredentialWireAccess:
    __slots__ = ("_endpoint_id", "_slot_id")

    def __init__(self, endpoint_id: str, slot_id: str) -> None:
        self._endpoint_id = endpoint_id
        self._slot_id = slot_id

    def permits(self, endpoint_id: str, slot_id: str) -> bool:
        return self._endpoint_id == endpoint_id and self._slot_id == slot_id


class CredentialMaterial:
    __slots__ = ("_endpoint_id", "_released", "_slot_id", "_value")

    def __init__(self, endpoint_id: str, slot_id: str, value: str) -> None:
        self._endpoint_id = endpoint_id
        self._slot_id = slot_id
        self._value = bytearray(value.encode("utf-8"))
        self._released = False

    def __repr__(self) -> str:
        return "CredentialMaterial(<redacted>)"

    def __str__(self) -> str:
        return "<redacted>"

    def __reduce__(self):
        raise TypeError("CredentialMaterial cannot be pickled")

    def read_for_wire(self, access: CredentialWireAccess) -> str:
        if self._released:
            raise RuntimeError("credential material is released")
        if not access.permits(self._endpoint_id, self._slot_id):
            raise PermissionError("credential wire access does not match endpoint slot")
        return self._value.decode("utf-8")

    def release(self) -> None:
        if self._released:
            return
        for index in range(len(self._value)):
            self._value[index] = 0
        self._value.clear()
        self._released = True


class CredentialLease(Protocol):
    async def resolve(self) -> CredentialMaterial:
        ...

    async def refresh(self) -> CredentialMaterial:
        ...

    async def release(self) -> None:
        ...


class SecretHandle(Protocol):
    @property
    def volatile(self) -> bool:
        ...

    @property
    def identity(self) -> SecretIdentity:
        ...

    @property
    def epoch(self) -> CredentialEpoch:
        ...

    async def acquire(self) -> CredentialLease:
        ...

    async def aclose(self) -> None:
        ...


class InMemoryCredentialLease:
    __slots__ = ("_endpoint_id", "_released", "_slot_id", "_value")

    def __init__(self, endpoint_id: str, slot_id: str, value: str) -> None:
        self._endpoint_id = endpoint_id
        self._slot_id = slot_id
        self._value = value
        self._released = False

    async def resolve(self) -> CredentialMaterial:
        if self._released:
            raise RuntimeError("credential lease is released")
        return CredentialMaterial(self._endpoint_id, self._slot_id, self._value)

    async def refresh(self) -> CredentialMaterial:
        return await self.resolve()

    async def release(self) -> None:
        self._released = True
        self._value = ""


class InMemorySecretHandle:
    __slots__ = (
        "_closed",
        "_endpoint_id",
        "_epoch",
        "_identity",
        "_lock",
        "_slot_id",
        "_value",
    )

    def __init__(
        self,
        *,
        endpoint_id: str,
        slot_id: str,
        identity: SecretIdentity,
        epoch: CredentialEpoch,
        value: str,
    ) -> None:
        self._endpoint_id = endpoint_id
        self._slot_id = slot_id
        self._identity = identity
        self._epoch = epoch
        self._value = value
        self._closed = False
        self._lock = asyncio.Lock()

    def __repr__(self) -> str:
        return "InMemorySecretHandle(<redacted>)"

    @property
    def volatile(self) -> bool:
        return False

    @property
    def identity(self) -> SecretIdentity:
        return self._identity

    @property
    def epoch(self) -> CredentialEpoch:
        return self._epoch

    async def acquire(self) -> CredentialLease:
        async with self._lock:
            if self._closed:
                raise RuntimeError("secret handle is closed")
            return InMemoryCredentialLease(self._endpoint_id, self._slot_id, self._value)

    async def aclose(self) -> None:
        async with self._lock:
            if self._closed:
                return
            self._closed = True
            self._value = ""


__all__ = [
    "CredentialEpoch",
    "CredentialLease",
    "CredentialMaterial",
    "CredentialWireAccess",
    "InMemorySecretHandle",
    "SecretHandle",
    "SecretIdentity",
]
