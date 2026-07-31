"""Logical-call cache boundary; never a provider-attempt authority."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from mote.contracts.model.invocation import CanonicalModelResponse


@dataclass(frozen=True, slots=True)
class SemanticCacheCandidate:
    candidate_id: str
    score: float
    response: CanonicalModelResponse


class SemanticInferenceCache(Protocol):
    async def lookup(
        self,
        embedding: tuple[float, ...],
        *,
        tenant_id: str,
        namespace: str,
        generation_revision: str,
        policy_revision: str,
        limit: int,
    ) -> tuple[SemanticCacheCandidate, ...]:
        ...

    async def put(
        self,
        embedding: tuple[float, ...],
        response: CanonicalModelResponse,
        *,
        tenant_id: str,
        namespace: str,
        generation_revision: str,
        policy_revision: str,
    ) -> None:
        ...

    async def delete_namespace(self, tenant_id: str, namespace: str) -> int:
        ...


class InferenceCache(Protocol):
    async def get(self, key: str, *, now: datetime) -> CanonicalModelResponse | None:
        ...

    async def put(
        self,
        key: str,
        response: CanonicalModelResponse,
        *,
        tenant_id: str,
        namespace: str,
        expires_at: datetime,
    ) -> None:
        ...

    async def delete_namespace(self, tenant_id: str, namespace: str) -> int:
        ...


__all__ = [
    "InferenceCache",
    "SemanticCacheCandidate",
    "SemanticInferenceCache",
]
