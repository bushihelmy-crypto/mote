#!/usr/bin/env python
# -*- coding: utf-8 -*-
from mote.common.base.singleton import Singleton
from mote.common.config.config.llm_config import LLMConfig, LLMType
from mote.common.const.llm import supports_native_tool_search
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


def _is_anthropic_endpoint(base_url: str) -> bool:
    """Whether ``base_url`` speaks the native Anthropic Messages protocol.

    Two shapes qualify: the first-party ``anthropic.com`` host, and any gateway
    whose path ends with ``/anthropic`` — the convention several Chinese vendors
    use to expose an Anthropic-compatible surface alongside their OpenAI one
    (MiniMax ``https://api.minimax.io/anthropic`` and its CN mirror
    ``api.minimaxi.com/anthropic``; Moonshot's Kimi ``/anthropic`` coding
    endpoint). A trailing slash is tolerated. The OpenAI-compatible ``/v1``
    surface of those same vendors does NOT match, so only configs explicitly
    pointed at the ``/anthropic`` surface take the native transport.
    """
    url = (base_url or "").lower().rstrip("/")
    if "anthropic.com" in url:
        return True
    return url.endswith("/anthropic")


def resolve_api_type(config: LLMConfig) -> LLMType:
    """Resolve the effective provider key, applying provider auto-detection.

    An explicit ``api_type: anthropic`` always selects the native Messages API
    client. Otherwise a ``base_url`` on the native Anthropic surface is treated
    as the native endpoint too (see :func:`_is_anthropic_endpoint`: the
    first-party ``anthropic.com`` host, plus any ``/anthropic``-suffixed gateway
    such as MiniMax / Kimi-coding). A Claude model reached via an
    OpenAI-compatible gateway (a non-Anthropic ``base_url``) keeps using the
    OpenAI client, while a direct ``https://api.anthropic.com`` config gets the
    native client without needing to set ``api_type`` by hand.

    DeepSeek models are auto-detected by model name (``deepseek`` substring) so
    the DeepSeek provider — which salvages tool calls the model occasionally
    leaks as DSML text — is selected even when reached via a shared
    OpenAI-compatible gateway (``api_type: openai``). The salvage only fires when
    structured ``tool_calls`` are empty AND the content holds a DSML block, so
    routing a non-DeepSeek model here by mistake is harmless. An explicit
    ``api_type: deepseek`` selects it directly.

    A genuine OpenAI config (``api_type: openai`` at an ``api.openai.com``
    ``base_url``) running a native-tool-search-capable model (gpt-5.4+, see
    :func:`supports_native_tool_search`) resolves to the ``OPENAI_RESPONSES``
    transport — a whole-model takeover onto the Responses API, the only OpenAI
    endpoint exposing native ``tool_search``. Older OpenAI models stay on Chat
    Completions (``OPENAI``); OpenAI-compatible gateways (groq/xai/… at their own
    ``base_url``) are excluded by the host check and keep Chat Completions too
    (they do not expose the Responses API).
    """
    if config.api_type == LLMType.ANTHROPIC:
        return LLMType.ANTHROPIC
    if _is_anthropic_endpoint(config.base_url or ""):
        return LLMType.ANTHROPIC
    if config.api_type == LLMType.DEEPSEEK:
        return LLMType.DEEPSEEK
    if "deepseek" in (config.model or "").lower():
        return LLMType.DEEPSEEK
    if (
        config.api_type == LLMType.OPENAI
        and "openai.com" in (config.base_url or "").lower()
        and supports_native_tool_search(config.model)
    ):
        return LLMType.OPENAI_RESPONSES
    return config.api_type


def create_llm_instance(config: LLMConfig) -> BaseLLM:
    """get the default llm provider"""
    return LLM_REGISTRY.get_provider(resolve_api_type(config))(config)


# Registry instance
LLM_REGISTRY = LLMProviderRegistry()
