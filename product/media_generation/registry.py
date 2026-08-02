"""Application-owned single-wire media endpoint catalog."""

from __future__ import annotations

from abc import ABC, abstractmethod
from types import MappingProxyType
from typing import ClassVar, Literal, TypeAlias

from mote.contracts.service import MediaGenerationResult, MediaGenerationSpec
from mote.contracts.tool.errors import ToolNotConfiguredError
from mote.product.config.multimodal import (
    AudioGenerationConfig,
    ImageGenerationConfig,
    MusicGenerationConfig,
    VideoGenerationConfig,
)

MediaKind: TypeAlias = Literal["image", "audio", "music", "video"]
MediaProviderConfig: TypeAlias = (
    ImageGenerationConfig | AudioGenerationConfig | MusicGenerationConfig | VideoGenerationConfig
)


class MediaProvider(ABC):
    """One media backend whose methods each perform at most one lifecycle wire."""

    kind: ClassVar[MediaKind]
    provider: ClassVar[str] = ""

    def __init__(self, config: MediaProviderConfig) -> None:
        self._config = config

    @abstractmethod
    async def start_once(
        self,
        item: MediaGenerationSpec,
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
        filename: str,
        *,
        timeout_seconds: float,
    ) -> MediaGenerationResult | None:
        """Poll once; return a completed asset or ``None`` while pending."""
        raise NotImplementedError

    async def reconcile_once(
        self,
        item: MediaGenerationSpec,
        *,
        idempotency_key: str,
        timeout_seconds: float,
    ) -> tuple[str, MediaGenerationResult | None] | None:
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
        self._providers: dict[tuple[MediaKind, str], type[MediaProvider]] = {}

    @property
    def snapshot(self) -> MappingProxyType[tuple[MediaKind, str], type[MediaProvider]]:
        return MappingProxyType(self._providers)

    def register(self, kind: MediaKind, name: str, provider_cls: type[MediaProvider]) -> None:
        key = (kind, name)
        existing = self._providers.get(key)
        if existing is not None and existing is not provider_cls:
            raise ValueError(f"Media provider {key!r} is already registered by {existing!r}")
        self._providers[key] = provider_cls

    def get_provider(self, kind: MediaKind, name: str) -> type[MediaProvider]:
        try:
            return self._providers[(kind, name)]
        except KeyError as e:
            available = sorted(n for (k, n) in self._providers if k == kind)
            raise ToolNotConfiguredError(
                f"No media provider {name!r} registered for kind {kind!r}. "
                f"Set multimodal.{kind}_generation.provider to one of: {available}."
            ) from e

    def create(
        self,
        kind: MediaKind,
        config: MediaProviderConfig,
    ) -> MediaProvider:
        """Construct one provider from this explicit catalog and resolved config."""

        return self.get_provider(kind, config.provider)(config)


def media_provider(kind: MediaKind, name: str):
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
    "MediaProviderConfig",
    "MediaKind",
    "MediaProviderRegistry",
    "media_provider",
]
