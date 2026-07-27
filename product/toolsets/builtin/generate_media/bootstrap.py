"""Explicit builtin media-provider catalog construction."""

from mote.product.toolsets.builtin.generate_media.creators import AudioCreator, ImageCreator, MusicCreator, VideoCreator
from mote.product.toolsets.builtin.generate_media.registry import MediaProviderRegistry

BUILTIN_MEDIA_PROVIDERS = (
    AudioCreator,
    MusicCreator,
    ImageCreator,
    VideoCreator,
)


def builtin_media_provider_registry() -> MediaProviderRegistry:
    registry = MediaProviderRegistry()
    for provider in BUILTIN_MEDIA_PROVIDERS:
        registry.register(provider.kind, provider.provider, provider)
    return registry


__all__ = ["BUILTIN_MEDIA_PROVIDERS", "builtin_media_provider_registry"]
