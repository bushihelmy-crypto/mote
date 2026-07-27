"""Application-owned single-wire media endpoint catalog."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, ClassVar

from mote.runtime.errors import ToolNotConfiguredError


class MediaProvider(ABC):
    """One media backend whose methods each perform at most one lifecycle wire."""

    kind: ClassVar[str] = ""
    provider: ClassVar[str] = ""

    def __init__(self, config: Any) -> None:
        self._config = config

    @abstractmethod
    async def start_once(
        self,
        item: dict[str, Any],
        *,
        idempotency_key: str,
        timeout_seconds: float,
    ) -> str:
        """Submit one asset and return its remote operation ID."""
        raise NotImplementedError

    @abstractmethod
    async def poll_once(
        self,
        operation_id: str,
        state: dict[str, Any],
        *,
        timeout_seconds: float,
    ) -> dict[str, Any] | None:
        """Poll once; return a completed asset or ``None`` while pending."""
        raise NotImplementedError

    async def reconcile_once(
        self,
        item: dict[str, Any],
        *,
        idempotency_key: str,
        timeout_seconds: float,
    ) -> tuple[str, dict[str, Any] | None] | None:
        """Recover a submit whose response was lost, when the vendor supports it."""
        return None

    async def cancel_once(
        self,
        operation_id: str,
        *,
        timeout_seconds: float,
    ) -> None:
        """Cancel one accepted operation when supported by the vendor."""
        raise NotImplementedError("media provider does not support cancellation")


class MediaProviderRegistry:
    """Isolated map ``(kind, name) -> MediaProvider subclass``."""

    def __init__(self) -> None:
        self.providers: dict[tuple[str, str], type[MediaProvider]] = {}

    def register(self, kind: str, name: str, provider_cls: type[MediaProvider]) -> None:
        key = (kind, name)
        existing = self.providers.get(key)
        if existing is not None and existing is not provider_cls:
            raise ValueError(f"Media provider {key!r} is already registered by {existing!r}")
        self.providers[key] = provider_cls

    def get_provider(self, kind: str, name: str) -> type[MediaProvider]:
        try:
            return self.providers[(kind, name)]
        except KeyError as e:
            available = sorted(n for (k, n) in self.providers if k == kind)
            raise ToolNotConfiguredError(
                f"No media provider {name!r} registered for kind {kind!r}. "
                f"Set multimodal.{kind}_generation.provider to one of: {available}."
            ) from e

    def create(
        self,
        kind: str,
        config: Any,
    ) -> MediaProvider:
        """Construct one provider from this explicit catalog and resolved config."""

        name = getattr(config, "provider", "openai") or "openai"
        return self.get_provider(kind, name)(config)


def media_provider(kind: str, name: str):
    """Declare provider identity without mutating a process-global catalog.

    Also stamps ``cls.kind`` / ``cls.provider`` so the decorator args are the
    single source of truth. An Application explicitly registers the resulting
    class in its own :class:`MediaProviderRegistry`.
    """

    def decorator(cls: type[MediaProvider]) -> type[MediaProvider]:
        cls.kind = kind
        cls.provider = name
        return cls

    return decorator


__all__ = [
    "MediaProvider",
    "MediaProviderRegistry",
    "media_provider",
]
