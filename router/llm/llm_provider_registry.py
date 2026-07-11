#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@Time    : 2023/12/19 17:26
@Author  : alexanderwu
@File    : llm_provider_registry.py
"""
from mote.common.base.singleton import Singleton
from mote.common.config.config.llm_config import LLMConfig, LLMType
from mote.common.exception import ProviderNotFoundError
from mote.router.llm.base_llm import BaseLLM


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
    """Resolve the effective provider key, applying provider auto-detection.

    An explicit ``api_type: anthropic`` always selects the native Messages API
    client. Otherwise a ``base_url`` pointing at ``anthropic.com`` is treated as
    the native endpoint too — so a Claude model reached via an OpenAI-compatible
    gateway (a non-Anthropic ``base_url``) keeps using the OpenAI client, while a
    direct ``https://api.anthropic.com`` config gets the native client without
    needing to set ``api_type`` by hand.

    DeepSeek models are auto-detected by model name (``deepseek`` substring) so
    the DeepSeek provider — which salvages tool calls the model occasionally
    leaks as DSML text — is selected even when reached via a shared
    OpenAI-compatible gateway (``api_type: openai``). The salvage only fires when
    structured ``tool_calls`` are empty AND the content holds a DSML block, so
    routing a non-DeepSeek model here by mistake is harmless. An explicit
    ``api_type: deepseek`` selects it directly.
    """
    if config.api_type == LLMType.ANTHROPIC:
        return LLMType.ANTHROPIC
    if "anthropic.com" in (config.base_url or "").lower():
        return LLMType.ANTHROPIC
    if config.api_type == LLMType.DEEPSEEK:
        return LLMType.DEEPSEEK
    if "deepseek" in (config.model or "").lower():
        return LLMType.DEEPSEEK
    return config.api_type


def create_llm_instance(config: LLMConfig) -> BaseLLM:
    """get the default llm provider"""
    return LLM_REGISTRY.get_provider(resolve_api_type(config))(config)


# Registry instance
LLM_REGISTRY = LLMProviderRegistry()
