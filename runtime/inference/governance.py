"""Independent quota and credential-health authorities."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Callable

from mote.contracts.inference.governance import (
    CredentialHealthObservation,
    CredentialHealthVerdict,
    ProviderQuotaObservation,
    QuotaObservationKind,
)


@dataclass(frozen=True, slots=True)
class _QuotaKey:
    provider: str
    endpoint_id: str
    credential_slot_id: str


@dataclass
class _QuotaState:
    remaining_requests: int | None = None
    remaining_tokens: int | None = None
    cooldown_until: float = 0.0


class ProviderQuotaAuthority:
    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._states: dict[_QuotaKey, _QuotaState] = {}
        self._lock = asyncio.Lock()

    async def allow(
        self,
        provider: str,
        endpoint_id: str,
        credential_slot_id: str,
        *,
        estimated_tokens: int,
    ) -> bool:
        if estimated_tokens < 0:
            raise ValueError("estimated tokens cannot be negative")
        key = _QuotaKey(provider, endpoint_id, credential_slot_id)
        async with self._lock:
            state = self._states.get(key)
            if state is None:
                return True
            if state.cooldown_until > self._clock():
                return False
            if state.remaining_requests is not None and state.remaining_requests <= 0:
                return False
            if state.remaining_tokens is not None and state.remaining_tokens < estimated_tokens:
                return False
            return True

    async def observe(self, observation: ProviderQuotaObservation) -> None:
        key = _QuotaKey(
            observation.provider,
            observation.endpoint_id,
            observation.credential_slot_id,
        )
        async with self._lock:
            state = self._states.setdefault(key, _QuotaState())
            if observation.kind is QuotaObservationKind.MALFORMED:
                return
            if observation.remaining_requests is not None:
                state.remaining_requests = observation.remaining_requests
            if observation.remaining_tokens is not None:
                state.remaining_tokens = observation.remaining_tokens
            if observation.kind in {QuotaObservationKind.RETRY_AFTER, QuotaObservationKind.EXHAUSTED}:
                retry_after = observation.retry_after_seconds or 1.0
                state.cooldown_until = max(state.cooldown_until, self._clock() + retry_after)


@dataclass
class _CredentialState:
    quarantine_until: float = 0.0
    revoked: bool = False
    refresh_required: bool = False


class CredentialHealthAuthority:
    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._states: dict[tuple[str, str], _CredentialState] = {}
        self._lock = asyncio.Lock()

    async def allow(self, credential_slot_id: str, credential_version: str) -> bool:
        async with self._lock:
            state = self._states.get((credential_slot_id, credential_version))
            return state is None or (
                not state.revoked and not state.refresh_required and state.quarantine_until <= self._clock()
            )

    async def observe(self, observation: CredentialHealthObservation) -> None:
        key = (observation.credential_slot_id, observation.credential_version)
        async with self._lock:
            state = self._states.setdefault(key, _CredentialState())
            if observation.verdict is CredentialHealthVerdict.SUCCESS:
                state.quarantine_until = 0.0
                state.refresh_required = False
            elif observation.verdict is CredentialHealthVerdict.REFRESH:
                state.refresh_required = True
            elif observation.verdict is CredentialHealthVerdict.QUARANTINE:
                state.quarantine_until = max(
                    state.quarantine_until,
                    self._clock() + (observation.quarantine_seconds or 1.0),
                )
            elif observation.verdict is CredentialHealthVerdict.REVOKE:
                state.revoked = True
