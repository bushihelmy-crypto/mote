"""SearchBackend registry — pluggable web-search backends.

The web-search backend is a swappable strategy selected by config, in the same
shape as the LLM provider registry
(:mod:`mote.router.llm.llm_provider_registry`) and the MediaProvider registry
(:mod:`mote.executor.tools.generate_media.registry`): a
``@register_search_backend(name)`` decorator populates a singleton keyed by
name, and :func:`create_search_backend` resolves the active backend from
``config.tools.web_search.backend``.

Today the sole built-in backend is ``"provider"`` — it wraps the Role's
provider-native server-side web search (Anthropic ``web_search_20250305`` /
OpenAI Responses ``web_search``). Adding a direct-API vendor
(Google/Tavily/Brave/SearXNG) is a new file + a
``@register_search_backend("google")`` decorator reading
``config.tools.web_search.{api_key,base_url}`` + pointing the config ``backend``
field at it — zero changes here or at the WebSearch tool call site. This is the
"leave the vendor entry point, config-select it" seam the roadmap calls for.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any, ClassVar, Optional

from mote.common.base.singleton import Singleton
from mote.common.exception import ToolNotConfiguredError

if TYPE_CHECKING:
    from mote.executor.capability_types import WebSearch as WebSearchCapability
    from mote.router.llm.llm_response import WebSearchHit


class SearchBackend(ABC):
    """A web-search backend: ``query`` → list of ranked hits.

    Constructed with its resolved ``config.tools.web_search`` sub-config plus an
    optional ``provider_search`` callable (the Role's provider-native search
    capability) that the built-in ``"provider"`` backend delegates to. A
    direct-API vendor backend ignores ``provider_search`` and uses the config's
    ``api_key``/``base_url`` instead. ``name`` is stamped by
    :func:`register_search_backend` (single source = the decorator arg).
    """

    name: ClassVar[str] = ""

    def __init__(self, config: Any, provider_search: "Optional[WebSearchCapability]" = None) -> None:
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


class SearchBackendRegistry(metaclass=Singleton):
    """Singleton map ``name -> SearchBackend subclass``."""

    def __init__(self) -> None:
        self.backends: dict[str, type[SearchBackend]] = {}

    def register(self, name: str, backend_cls: type[SearchBackend]) -> None:
        self.backends[name] = backend_cls

    def get_backend(self, name: str) -> type[SearchBackend]:
        try:
            return self.backends[name]
        except KeyError as e:
            available = sorted(self.backends)
            raise ToolNotConfiguredError(
                f"No web-search backend {name!r} registered. " f"Set tools.web_search.backend to one of: {available}."
            ) from e


# Registry instance.
SEARCH_REGISTRY = SearchBackendRegistry()


def register_search_backend(name: str):
    """Register a :class:`SearchBackend` subclass under ``name``.

    Also stamps ``cls.name`` so the decorator arg is the single source of truth.
    """

    def decorator(cls: type[SearchBackend]) -> type[SearchBackend]:
        cls.name = name
        SEARCH_REGISTRY.register(name, cls)
        return cls

    return decorator


def create_search_backend(config: Any, *, provider_search: "Optional[WebSearchCapability]" = None) -> SearchBackend:
    """Construct the active backend from ``config.tools.web_search``.

    Selects the backend named by the config's ``backend`` field (default
    ``"provider"``) and constructs it with that config + the provider-native
    ``provider_search`` capability. Raises :class:`ToolNotConfiguredError` naming
    the config path when the selected backend is not registered.
    """
    name = getattr(config, "backend", "provider") or "provider"
    cls = SEARCH_REGISTRY.get_backend(name)
    return cls(config, provider_search)


@register_search_backend("provider")
class ProviderSearchBackend(SearchBackend):
    """Built-in backend = the routed model's provider-native server-side search.

    Delegates to the Role's ``web_search`` capability (Anthropic
    ``web_search_20250305`` / OpenAI Responses ``web_search``). Raises
    ``NotImplementedError`` when no capability is bound or the routed model has no
    server-side search — the WebSearch tool turns that into a
    :class:`ToolNotConfiguredError` steering the user to a search-capable model or
    the WebBrowser tool.
    """

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


__all__ = [
    "SearchBackend",
    "SearchBackendRegistry",
    "SEARCH_REGISTRY",
    "register_search_backend",
    "create_search_backend",
    "ProviderSearchBackend",
]
