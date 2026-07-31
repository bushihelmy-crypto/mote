"""Product media-generation providers, catalog, policy, and service adapter."""

from mote.product.media_generation.catalog import builtin_media_provider_registry
from mote.product.media_generation.registry import MediaProviderRegistry

__all__ = ["MediaProviderRegistry", "builtin_media_provider_registry"]
