#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@Time    : 2023/12/19 17:26
@Author  : alexanderwu
@File    : llm_provider_registry.py
"""
from metagpt.common.base.singleton import Singleton
from metagpt.common.config.config.llm_config import LLMConfig, LLMType
from metagpt.common.exception import ProviderNotFoundError
from metagpt.router.llm.base_llm import BaseLLM


class LLMProviderRegistry(metaclass=Singleton):
    def __init__(self):
        self.providers = {}

    def register(self, key, provider_cls):
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


def register_provider(keys):
    """register provider to registry"""

    def decorator(cls):
        if isinstance(keys, list):
            for key in keys:
                LLM_REGISTRY.register(key, cls)
        else:
            LLM_REGISTRY.register(keys, cls)
        return cls

    return decorator


def resolve_api_type(config: LLMConfig) -> LLMType:
    """Resolve the effective provider key, applying native-Anthropic auto-detection.

    An explicit ``api_type: anthropic`` always selects the native Messages API
    client. Otherwise a ``base_url`` pointing at ``anthropic.com`` is treated as
    the native endpoint too — so a Claude model reached via an OpenAI-compatible
    gateway (a non-Anthropic ``base_url``) keeps using the OpenAI client, while a
    direct ``https://api.anthropic.com`` config gets the native client without
    needing to set ``api_type`` by hand.
    """
    if config.api_type == LLMType.ANTHROPIC:
        return LLMType.ANTHROPIC
    if "anthropic.com" in (config.base_url or "").lower():
        return LLMType.ANTHROPIC
    return config.api_type


def create_llm_instance(config: LLMConfig) -> BaseLLM:
    """get the default llm provider"""
    return LLM_REGISTRY.get_provider(resolve_api_type(config))(config)


# Registry instance
LLM_REGISTRY = LLMProviderRegistry()
