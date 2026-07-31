"""Tenant-isolated exact response cache semantics."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone

from mote.contracts.model.invocation import CanonicalModelResponse, ModelInvocation
from mote.contracts.ports.inference.cache import InferenceCache


@dataclass(frozen=True, slots=True)
class ExactCacheIdentity:
    tenant_id: str
    namespace: str
    generation_revision: str
    policy_revision: str
    model_capability_identity: str

    def __post_init__(self) -> None:
        if not all(
            (
                self.tenant_id,
                self.namespace,
                self.generation_revision,
                self.policy_revision,
                self.model_capability_identity,
            )
        ):
            raise ValueError("exact cache identity is incomplete")


def exact_cache_key(identity: ExactCacheIdentity, invocation: ModelInvocation) -> str:
    canonical = {
        "tenant_id": identity.tenant_id,
        "namespace": identity.namespace,
        "generation_revision": identity.generation_revision,
        "policy_revision": identity.policy_revision,
        "model_capability_identity": identity.model_capability_identity,
        "route_id": invocation.route_id.model_dump(mode="json"),
        "operation": invocation.operation.value,
        "input": invocation.input.model_dump(mode="json"),
        "requirements": invocation.requirements.model_dump(mode="json"),
    }
    payload = json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode()
    return "sha256:" + hashlib.sha256(payload).hexdigest()


@dataclass(frozen=True, slots=True)
class _Entry:
    tenant_id: str
    namespace: str
    response: CanonicalModelResponse
    expires_at: datetime


class MemoryExactInferenceCache(InferenceCache):
    """Bounded-process adapter used for Embedded tests and composition."""

    def __init__(self, *, maximum_entries: int = 1000) -> None:
        if maximum_entries <= 0:
            raise ValueError("exact cache maximum entries must be positive")
        self._maximum_entries = maximum_entries
        self._entries: dict[str, _Entry] = {}

    async def get(self, key: str, *, now: datetime | None = None) -> CanonicalModelResponse | None:
        instant = now or datetime.now(timezone.utc)
        entry = self._entries.get(key)
        if entry is None:
            return None
        if entry.expires_at <= instant:
            del self._entries[key]
            return None
        return entry.response

    async def put(
        self,
        key: str,
        response: CanonicalModelResponse,
        *,
        tenant_id: str,
        namespace: str,
        expires_at: datetime,
    ) -> None:
        if not tenant_id or not namespace:
            raise ValueError("exact cache tenant and namespace are required")
        if expires_at.tzinfo is None:
            raise ValueError("exact cache expiry must be timezone-aware")
        if key not in self._entries and len(self._entries) >= self._maximum_entries:
            oldest = min(self._entries, key=lambda item: self._entries[item].expires_at)
            del self._entries[oldest]
        self._entries[key] = _Entry(tenant_id, namespace, response, expires_at)

    async def delete_namespace(self, tenant_id: str, namespace: str) -> int:
        selected = tuple(
            key for key, entry in self._entries.items() if entry.tenant_id == tenant_id and entry.namespace == namespace
        )
        for key in selected:
            del self._entries[key]
        return len(selected)


__all__ = ["ExactCacheIdentity", "MemoryExactInferenceCache", "exact_cache_key"]
