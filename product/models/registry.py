"""Product-owned provider factory catalog."""

from __future__ import annotations

from pathlib import Path

from mote.contracts.config.model.llm import LLMConfig, LLMType
from mote.contracts.model.transport import resolve_api_type
from mote.product.models.errors import ProviderNotFoundError
from mote.runtime.models.clients.base import BaseLLM


class LLMProviderRegistry:
    def __init__(self, *, oauth_root: Path | None = None):
        self.providers: dict[LLMType | str, type[BaseLLM]] = {}
        self._oauth_root = oauth_root

    def register(self, key, provider_cls) -> None:
        existing = self.providers.get(key)
        if existing is not None and existing is not provider_cls:
            raise ValueError(f"Provider key {key!r} is already registered by {existing!r}")
        self.providers[key] = provider_cls

    def get_provider(self, api_type: LLMType):
        try:
            return self.providers[api_type]
        except KeyError as exc:
            raise ProviderNotFoundError(
                f"No LLM provider registered for api_type {api_type!r}. "
                f"Registered: {sorted(str(key) for key in self.providers)}",
                api_type=str(api_type),
                cause=exc,
            ) from exc

    def create(self, config: LLMConfig) -> BaseLLM:
        if config.oauth is not None:
            if self._oauth_root is None:
                raise ValueError("OAuth-enabled providers require an OAuth root")
            config.oauth.storage_root = self._oauth_root
        return self.get_provider(resolve_api_type(config))(config)


__all__ = ["LLMProviderRegistry"]
