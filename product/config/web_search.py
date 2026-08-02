"""Product web-search service configuration.

Pure-data config model (no Runtime import). Mirrors :class:`DeviceConfig`'s home
in ``config.tools``: the Product ServiceEndpoint resolver selects the active
backend from here while the WebSearch Tool remains provider-neutral.

The sole built-in backend is ``"provider"`` — the provider-native server-side
search (Anthropic ``web_search_20250305`` / OpenAI Responses ``web_search``),
which needs no fields here (it rides the routed model). ``api_key``/``base_url``
are the entry points for a FUTURE direct-API vendor (Google/Tavily/Brave/SearXNG)
registered in the Product catalog plus ``backend: "google"`` — unused by
``"provider"``.
"""

from __future__ import annotations

from mote.contracts.config.base import ConfigModel


class WebSearchConfig(ConfigModel):
    """Settings for the WebSearch tool's pluggable search backend."""

    # Which registered SearchBackend drives web search. ``"provider"`` (default)
    # uses the routed model's server-side search; point this at another registered
    # backend name (e.g. ``"google"``) to swap in a direct-API vendor.
    backend: str = "provider"

    # Credentials for a direct-API backend (Google/Tavily/...). Unused by the
    # built-in ``"provider"`` backend, which authenticates via the routed model.
    api_key: str = ""
    base_url: str = ""


__all__ = ["WebSearchConfig"]
