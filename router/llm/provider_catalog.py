#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Provider catalog: data-only brand presets for LLM configuration.

A *brand* (``provider``) such as ``deepseek`` or ``groq`` is distinct from its
*wire protocol* (``api_type``): most brands speak the OpenAI-compatible wire
(``openai_api.py``) at a brand-specific ``base_url``, while ``anthropic`` speaks
the native Messages API. A :class:`ProviderPreset` captures that mapping plus the
environment variables that may hold an API key and an optional link to an OAuth
provider preset.

Mirrors ``router/oauth/registry.py``: the catalog only fills values the user
left empty (user always wins) and never imports anything *up* the dependency
graph, so ``LLMConfig`` can lazy-import it from ``common`` without a cycle.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from metagpt.common.config.config.llm_config import LLMType


@dataclass(frozen=True)
class ProviderPreset:
    """Public, brand-specific defaults applied to an ``LLMConfig``.

    ``base_url`` + ``api_type`` (wire protocol) are filled when the user did not
    set them. ``env_keys`` lists the environment variables searched (in order)
    for an API key. ``oauth_provider`` links to a preset in
    ``router/oauth/registry.py`` when the brand supports OAuth login.
    """

    base_url: str
    api_type: LLMType
    env_keys: List[str] = field(default_factory=list)
    default_model: Optional[str] = None
    oauth_provider: Optional[str] = None


# brand name -> preset. Most brands use the OpenAI-compatible wire with a
# brand-specific base_url; brands that already have a dedicated LLMType keep it
# (so cost-manager selection is unchanged); anthropic uses the native wire.
PROVIDER_CATALOG: Dict[str, ProviderPreset] = {
    "openai": ProviderPreset(
        base_url="https://api.openai.com/v1",
        api_type=LLMType.OPENAI,
        env_keys=["OPENAI_API_KEY"],
        oauth_provider="openai",
    ),
    "anthropic": ProviderPreset(
        base_url="https://api.anthropic.com",
        api_type=LLMType.ANTHROPIC,
        env_keys=["ANTHROPIC_API_KEY"],
        oauth_provider="anthropic",
    ),
    "deepseek": ProviderPreset(
        base_url="https://api.deepseek.com/v1",
        api_type=LLMType.DEEPSEEK,
        env_keys=["DEEPSEEK_API_KEY"],
    ),
    "moonshot": ProviderPreset(
        base_url="https://api.moonshot.cn/v1",
        api_type=LLMType.MOONSHOT,
        env_keys=["MOONSHOT_API_KEY"],
    ),
    "mistral": ProviderPreset(
        base_url="https://api.mistral.ai/v1",
        api_type=LLMType.MISTRAL,
        env_keys=["MISTRAL_API_KEY"],
    ),
    "yi": ProviderPreset(
        base_url="https://api.lingyiwanwu.com/v1",
        api_type=LLMType.YI,
        env_keys=["YI_API_KEY"],
    ),
    "open_router": ProviderPreset(
        base_url="https://openrouter.ai/api/v1",
        api_type=LLMType.OPEN_ROUTER,
        env_keys=["OPENROUTER_API_KEY"],
    ),
    "siliconflow": ProviderPreset(
        base_url="https://api.siliconflow.cn/v1",
        api_type=LLMType.SILICONFLOW,
        env_keys=["SILICONFLOW_API_KEY"],
    ),
    "fireworks": ProviderPreset(
        base_url="https://api.fireworks.ai/inference/v1",
        api_type=LLMType.FIREWORKS,
        env_keys=["FIREWORKS_API_KEY"],
    ),
    "open_llm": ProviderPreset(
        base_url="http://localhost:8000/v1",
        api_type=LLMType.OPEN_LLM,
        env_keys=["OPEN_LLM_API_KEY"],
    ),
    "groq": ProviderPreset(
        base_url="https://api.groq.com/openai/v1",
        api_type=LLMType.OPENAI,
        env_keys=["GROQ_API_KEY"],
    ),
    "xai": ProviderPreset(
        base_url="https://api.x.ai/v1",
        api_type=LLMType.OPENAI,
        env_keys=["XAI_API_KEY"],
    ),
    "together": ProviderPreset(
        base_url="https://api.together.xyz/v1",
        api_type=LLMType.OPENAI,
        env_keys=["TOGETHER_API_KEY"],
    ),
    "nvidia": ProviderPreset(
        base_url="https://integrate.api.nvidia.com/v1",
        api_type=LLMType.OPENAI,
        env_keys=["NVIDIA_API_KEY"],
    ),
    "cerebras": ProviderPreset(
        base_url="https://api.cerebras.ai/v1",
        api_type=LLMType.OPENAI,
        env_keys=["CEREBRAS_API_KEY"],
    ),
    "zai": ProviderPreset(
        base_url="https://api.z.ai/api/paas/v4",
        api_type=LLMType.OPENAI,
        env_keys=["ZAI_API_KEY"],
    ),
    "minimax": ProviderPreset(
        base_url="https://api.minimaxi.com/v1",
        api_type=LLMType.OPENAI,
        env_keys=["MINIMAX_API_KEY"],
    ),
    "dashscope": ProviderPreset(
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        api_type=LLMType.OPENAI,
        env_keys=["DASHSCOPE_API_KEY"],
    ),
    "zhipuai": ProviderPreset(
        base_url="https://open.bigmodel.cn/api/paas/v4",
        api_type=LLMType.OPENAI,
        env_keys=["ZHIPUAI_API_KEY"],
    ),
    "stepfun": ProviderPreset(
        base_url="https://api.stepfun.com/v1",
        api_type=LLMType.OPENAI,
        env_keys=["STEPFUN_API_KEY"],
    ),
    "baichuan": ProviderPreset(
        base_url="https://api.baichuan-ai.com/v1",
        api_type=LLMType.OPENAI,
        env_keys=["BAICHUAN_API_KEY"],
    ),
    "perplexity": ProviderPreset(
        base_url="https://api.perplexity.ai",
        api_type=LLMType.OPENAI,
        env_keys=["PERPLEXITY_API_KEY"],
    ),
    "github-copilot": ProviderPreset(
        base_url="https://api.individual.githubcopilot.com",
        api_type=LLMType.OPENAI,
        env_keys=["COPILOT_GITHUB_TOKEN"],
        oauth_provider="github-copilot",
    ),
}


def list_providers() -> List[str]:
    """Return the registered provider brand names (sorted)."""
    return sorted(PROVIDER_CATALOG)


def get_provider_preset(name: str) -> ProviderPreset:
    """Return the :class:`ProviderPreset` for ``name``.

    Raises ``KeyError`` (listing the known providers) when unknown. Matching is
    case-insensitive and whitespace-trimmed.
    """
    key = (name or "").strip().lower()
    if key not in PROVIDER_CATALOG:
        raise KeyError(f"unknown provider {name!r}; known: {list_providers()}")
    return PROVIDER_CATALOG[key]


def apply_provider_preset(values: dict) -> dict:
    """Fill ``base_url`` / ``api_type`` / oauth link from a brand preset (user wins).

    No-op when ``values`` has no ``provider`` key. Only fills fields the user
    left empty. When the preset names an ``oauth_provider`` and the user already
    supplied an ``oauth`` block *without* its own ``provider``, the brand's OAuth
    preset name is injected so endpoint metadata can resolve. Returns ``values``
    mutated in place for convenience.
    """
    provider = values.get("provider")
    if not provider:
        return values

    preset = get_provider_preset(provider)
    if values.get("base_url") in (None, ""):
        values["base_url"] = preset.base_url
    if values.get("api_type") in (None, ""):
        values["api_type"] = preset.api_type

    # Link the OAuth provider preset when the user opted into oauth but didn't
    # name a provider for it. Never overrides an explicit oauth.provider.
    if preset.oauth_provider:
        oauth = values.get("oauth")
        if isinstance(oauth, dict) and not oauth.get("provider"):
            oauth["provider"] = preset.oauth_provider
    return values


def find_env_keys(provider: str) -> Optional[List[str]]:
    """Return the configured env vars for ``provider`` that are actually set.

    ``None`` when the provider is unknown or none of its env vars are present.
    """
    try:
        preset = get_provider_preset(provider)
    except KeyError:
        return None
    found = [k for k in preset.env_keys if os.environ.get(k)]
    return found or None


def get_env_api_key(provider: str) -> Optional[str]:
    """Return the first set env-var value for ``provider`` (or ``None``)."""
    keys = find_env_keys(provider)
    if not keys:
        return None
    return os.environ.get(keys[0])


__all__ = [
    "ProviderPreset",
    "PROVIDER_CATALOG",
    "list_providers",
    "get_provider_preset",
    "apply_provider_preset",
    "find_env_keys",
    "get_env_api_key",
]
