"""Semantic-cache planning with governed, non-recursive embedding calls."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from mote.contracts.model.invocation import ModelInvocation
from mote.contracts.ports.inference.cache import SemanticCacheCandidate, SemanticInferenceCache
from mote.runtime.inference.cache import ExactCacheIdentity


@dataclass(frozen=True, slots=True)
class SemanticEmbeddingRequest:
    invocation: ModelInvocation
    origin: str = "semantic_cache_lookup"
    cache_mode: str = "bypass"


class SemanticCachePlanner:
    """Lookup planner; embedding execution remains a separately journaled owner."""

    def __init__(
        self,
        backend: SemanticInferenceCache,
        *,
        identity: ExactCacheIdentity,
        threshold: float,
        embed: Callable[[SemanticEmbeddingRequest], Awaitable[tuple[float, ...]]],
    ) -> None:
        if not 0 <= threshold <= 1:
            raise ValueError("semantic cache threshold must be between zero and one")
        self._backend = backend
        self._identity = identity
        self._threshold = threshold
        self._embed = embed

    async def lookup(self, invocation: ModelInvocation) -> SemanticCacheCandidate | None:
        embedding = await self._embed(SemanticEmbeddingRequest(invocation))
        if not embedding:
            raise ValueError("semantic cache embedding must not be empty")
        candidates = await self._backend.lookup(
            embedding,
            tenant_id=self._identity.tenant_id,
            namespace=self._identity.namespace,
            generation_revision=self._identity.generation_revision,
            policy_revision=self._identity.policy_revision,
            limit=1,
        )
        if not candidates or candidates[0].score < self._threshold:
            return None
        return candidates[0]


__all__ = ["SemanticCachePlanner", "SemanticEmbeddingRequest"]
