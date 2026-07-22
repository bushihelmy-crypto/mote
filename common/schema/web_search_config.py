"""WebSearchConfig — knobs for the WebSearch tool's pluggable search backend.

Pure-data config model (no executor import), lazy-exported from
``mote.common.schema``. Mirrors :class:`DeviceConfig`'s home in ``config.tools``:
the WebSearch tool resolves its active backend from here via the search-backend
registry (``executor/tools/web_search_registry.py``), exactly as the LLM clients
resolve a provider from ``models.*``.

The sole built-in backend is ``"provider"`` — the provider-native server-side
search (Anthropic ``web_search_20250305`` / OpenAI Responses ``web_search``),
which needs no fields here (it rides the routed model). ``api_key``/``base_url``
are the entry points for a FUTURE direct-API vendor (Google/Tavily/Brave/SearXNG)
added as ``@register_search_backend("google")`` + ``backend: "google"`` — unused
by ``"provider"``.
"""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class WebSearchConfig(BaseModel):
    """Settings for the WebSearch tool's pluggable search backend."""

    model_config = ConfigDict(extra="forbid")

    # Which registered SearchBackend drives web search. ``"provider"`` (default)
    # uses the routed model's server-side search; point this at another registered
    # backend name (e.g. ``"google"``) to swap in a direct-API vendor.
    backend: str = "provider"

    # Credentials for a direct-API backend (Google/Tavily/...). Unused by the
    # built-in ``"provider"`` backend, which authenticates via the routed model.
    api_key: str = ""
    base_url: str = ""


__all__ = ["WebSearchConfig"]
