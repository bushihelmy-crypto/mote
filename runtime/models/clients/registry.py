#!/usr/bin/env python
# -*- coding: utf-8 -*-
from mote.contracts.config.llm import LLMConfig, LLMType
from mote.contracts.models.transport import resolve_api_type
from mote.runtime.errors import ProviderNotFoundError
from mote.runtime.models.clients.base import BaseLLM


class LLMProviderRegistry:
    """An explicit provider catalog; instances never share hidden state."""

    def __init__(self):
        self.providers: dict[LLMType | str, type[BaseLLM]] = {}

    def register(self, key, provider_cls):
        existing = self.providers.get(key)
        if existing is not None and existing is not provider_cls:
            raise ValueError(f"Provider key {key!r} is already registered by {existing!r}")
        self.providers[key] = provider_cls

    def get_provider(self, enum: LLMType):
        """get provider instance according to the enum"""
        try:
            return self.providers[enum]
        except KeyError as e:
            raise ProviderNotFoundError(
                f"No LLM provider registered for api_type {enum!r}. "
                f"Registered: {sorted(str(k) for k in self.providers)}",
                api_type=str(enum),
                cause=e,
            ) from e

    def create(self, config: LLMConfig) -> BaseLLM:
        """Build one provider from this catalog."""
        return self.get_provider(resolve_api_type(config))(config)


__all__ = ["LLMProviderRegistry", "resolve_api_type"]
