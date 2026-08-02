"""Application-owned web-search backend catalog.

The web-search backend is a swappable strategy selected by config, in the same
shape as the LLM and MediaProvider registries. Each
:class:`ProductContainer` constructs an isolated catalog and injects its
snapshot into the Product service endpoint resolver.

Today the sole built-in backend is ``"provider"`` — it wraps the adapter's
provider-native server-side web search (Anthropic ``web_search_20250305`` /
OpenAI Responses ``web_search``). Adding a direct-API vendor
(Google/Tavily/Brave/SearXNG) is a new class explicitly registered in the
Application catalog, reading ``config.tools.web_search.{api_key,base_url}``,
plus a matching config ``backend`` value — zero changes at the WebSearch Tool
or Runtime gateway. This is the
"leave the vendor entry point, config-select it" seam the roadmap calls for.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping
from types import MappingProxyType
from typing import TYPE_CHECKING, ClassVar, Optional, Protocol

from mote.contracts.tool.errors import ToolNotConfiguredError
from mote.product.config.web_search import WebSearchConfig

if TYPE_CHECKING:
    from mote.contracts.model import WebSearchHit


class ProviderSearch(Protocol):
    async def __call__(
        self,
        query: str,
        *,
        allowed_domains: list[str] | None = None,
        blocked_domains: list[str] | None = None,
    ) -> "list[WebSearchHit]": ...


class SearchBackend(ABC):
    """A web-search backend: ``query`` → list of ranked hits.

    Constructed with its resolved ``config.tools.web_search`` sub-config plus an
    optional ``provider_search`` callable (the adapter's stable ModelGateway
    call) that the built-in ``"provider"`` backend delegates to. A
    direct-API vendor backend ignores ``provider_search`` and uses the config's
    ``api_key``/``base_url`` instead. Its ``name`` is the catalog key.
    """

    name: ClassVar[str] = ""

    def __init__(
        self,
        config: WebSearchConfig,
        provider_search: Optional[ProviderSearch] = None,
    ) -> None:
        self._config = config
        self._provider_search = provider_search

    @abstractmethod
    async def search(
        self,
        query: str,
        *,
        allowed_domains: Optional[list[str]] = None,
        blocked_domains: Optional[list[str]] = None,
    ) -> "list[WebSearchHit]":
        """Run the search and return the ranked hits."""
        raise NotImplementedError


class SearchBackendRegistry:
    """Isolated map ``name -> SearchBackend subclass``."""

    def __init__(self) -> None:
        self._backends: dict[str, type[SearchBackend]] = {}

    @property
    def backends(self) -> Mapping[str, type[SearchBackend]]:
        """Immutable diagnostic projection; mutation remains catalog-owned."""

        return MappingProxyType(self._backends)

    def register(self, name: str, backend_factory: type[SearchBackend]) -> None:
        if not name or name != name.strip():
            raise ValueError("Search backend identity must be a non-empty canonical name")
        if backend_factory.name != name:
            raise ValueError("Search backend registration identity does not match its declaration")
        existing = self._backends.get(name)
        if existing is not None and existing is not backend_factory:
            raise ValueError(f"Search backend {name!r} is already registered by {existing!r}")
        self._backends[name] = backend_factory

    def get_backend(self, name: str) -> type[SearchBackend]:
        try:
            return self._backends[name]
        except KeyError as e:
            available = sorted(self._backends)
            raise ToolNotConfiguredError(
                f"No web-search backend {name!r} registered. " f"Set tools.web_search.backend to one of: {available}."
            ) from e

    def create(
        self,
        config: WebSearchConfig,
        *,
        provider_search: Optional[ProviderSearch] = None,
    ) -> SearchBackend:
        if not isinstance(config, WebSearchConfig):
            raise TypeError("Search backend requires a validated WebSearchConfig")
        name = config.backend or "provider"
        backend = self.get_backend(name)(config, provider_search)
        if not isinstance(backend, SearchBackend):
            raise TypeError(f"Search backend factory {name!r} returned an invalid implementation")
        return backend


class ProviderSearchBackend(SearchBackend):
    """Built-in backend = the routed model's provider-native server-side search.

    Delegates to the adapter's ModelGateway search callable (Anthropic
    ``web_search_20250305`` / OpenAI Responses ``web_search``). Raises
    ``NotImplementedError`` when no provider search callable is bound.
    """

    name = "provider"

    async def search(
        self,
        query: str,
        *,
        allowed_domains: Optional[list[str]] = None,
        blocked_domains: Optional[list[str]] = None,
    ) -> "list[WebSearchHit]":
        if self._provider_search is None:
            raise NotImplementedError("No provider web-search capability bound.")
        return await self._provider_search(
            query,
            allowed_domains=allowed_domains,
            blocked_domains=blocked_domains,
        )


def builtin_search_backend_registry() -> SearchBackendRegistry:
    registry = SearchBackendRegistry()
    registry.register(ProviderSearchBackend.name, ProviderSearchBackend)
    return registry


__all__ = [
    "SearchBackend",
    "SearchBackendRegistry",
    "ProviderSearchBackend",
    "builtin_search_backend_registry",
]
