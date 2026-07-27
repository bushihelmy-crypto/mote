"""Product adapters for externally hosted Tool services."""

from mote.product.integrations.services.media import MediaServiceEndpointResolver, build_media_service_snapshot
from mote.product.integrations.services.resolver import ProductServiceEndpointResolver
from mote.product.integrations.services.web_search import (
    WebSearchServiceEndpointResolver,
    build_web_search_service_snapshot,
)

__all__ = [
    "MediaServiceEndpointResolver",
    "ProductServiceEndpointResolver",
    "WebSearchServiceEndpointResolver",
    "build_media_service_snapshot",
    "build_web_search_service_snapshot",
]
