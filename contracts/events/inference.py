"""Operational response-cache observations."""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar


@dataclass(frozen=True, slots=True)
class ModelCacheHitRecord:
    model_call_id: str
    cache_kind: str
    tenant_id: str
    namespace: str
    provider_request_id: None = None
    name: ClassVar[str] = "model_cache_hit"


@dataclass(frozen=True, slots=True)
class ModelCacheDegraded:
    operation: str
    cache_kind: str
    error_code: str
    name: ClassVar[str] = "model_cache_degraded"


__all__ = ["ModelCacheDegraded", "ModelCacheHitRecord"]
