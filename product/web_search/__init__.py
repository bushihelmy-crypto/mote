"""Product hosted Web Search backends and service adapter."""

from mote.product.web_search.registry import SearchBackendRegistry, builtin_search_backend_registry

__all__ = ["SearchBackendRegistry", "builtin_search_backend_registry"]
