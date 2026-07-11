#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@Time    : 2024/1/4 16:33
@Author  : alexanderwu
@File    : llm_config.py
"""
import os
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Union

from mote.common.config.config.oauth_config import OAuthProviderConfig
from mote.common.const import LLM_API_TIMEOUT
from mote.common.exception import MissingAPIKeyError
from mote.common.utils.yaml_model import YamlModel
from pydantic import field_validator, model_validator


class LLMType(Enum):
    """LLM types.

    All values except ``ANTHROPIC`` use the OpenAI-compatible client
    (``openai_api.py`` with a provider-specific ``base_url``). ``ANTHROPIC``
    selects the native Anthropic Messages API client (``anthropic_api.py``);
    it is also auto-detected when ``base_url`` points at ``anthropic.com``.
    """

    OPENAI = "openai"
    # OpenAI-compatible providers (use openai_api.py with different base_url)
    FIREWORKS = "fireworks"
    OPEN_LLM = "open_llm"
    MOONSHOT = "moonshot"
    MISTRAL = "mistral"
    YI = "yi"  # lingyiwanwu
    OPEN_ROUTER = "open_router"
    DEEPSEEK = "deepseek"
    SILICONFLOW = "siliconflow"
    # Native Anthropic Messages API (anthropic_api.py, direct /v1/messages).
    ANTHROPIC = "anthropic"

    def __missing__(self, key):
        return self.OPENAI


class LLMConfig(YamlModel):
    """Config for LLM

    OpenAI: https://github.com/openai/openai-python/blob/main/src/openai/resources/chat/completions.py#L681
    Optional Fields in pydantic: https://docs.pydantic.dev/latest/migration/#required-optional-and-nullable-fields
    """

    # Optional brand preset (e.g. 'deepseek', 'groq', 'anthropic'). When set, the
    # provider catalog fills base_url + api_type (wire protocol) + an oauth link
    # and resolves api_key from the brand's env vars — all only when the user
    # left those fields empty (explicit values always win). Configs without
    # ``provider`` behave exactly as before.
    provider: Optional[str] = None

    # A single key, or a list of keys to rotate through on auth/billing failures
    # (recovery=ROTATE_CREDENTIAL). The first key is used until one is exhausted.
    api_key: Union[str, List[str]] = "sk-"
    api_type: LLMType = LLMType.OPENAI
    base_url: str = "https://api.openai.com/v1"
    api_version: Optional[str] = None

    # Opt-in OAuth: when set, the bearer token is obtained/refreshed from an
    # OAuth token endpoint instead of using the static ``api_key``. Leaving this
    # ``None`` preserves the static-key path unchanged.
    oauth: Optional[OAuthProviderConfig] = None

    model: Optional[str] = None  # also stands for DEPLOYMENT_NAME
    pricing_plan: Optional[str] = None  # Cost Settlement Plan Parameters.

    # For Chat Completion
    max_token: int = 4096
    temperature: float = 0.0
    top_p: float = 1.0
    top_k: int = 0
    repetition_penalty: float = 1.0
    stop: Optional[str] = None
    presence_penalty: float = 0.0
    frequency_penalty: float = 0.0
    best_of: Optional[int] = None
    n: Optional[int] = None
    stream: bool = False
    logprobs: Optional[bool] = None  # https://cookbook.openai.com/examples/using_logprobs
    top_logprobs: Optional[int] = None
    timeout: int = 600

    # For Network
    proxy: Optional[str] = None

    # Cost Control
    calc_usage: bool = True

    # Anthropic prompt caching: place ``cache_control`` breakpoints on the stable
    # request prefixes (system / tools / conversation tail) so repeat turns are
    # billed at cheap cache-read rates. Native-Anthropic path only; ignored by the
    # OpenAI-compatible providers, which auto-cache and need no markers.
    use_prompt_cache: bool = True

    @model_validator(mode="before")
    @classmethod
    def _apply_provider_preset(cls, values: Any) -> Any:
        """Resolve a brand ``provider`` into base_url/api_type/oauth + env api_key.

        Uses the module-level provider catalog (co-located below) so there is no
        ``common -> router`` import cycle. Explicit user values always win; an
        absent/placeholder ``api_key`` is filled from the brand's env vars when
        one is set.
        """
        if not isinstance(values, dict) or not values.get("provider"):
            return values
        values = apply_provider_preset(values)
        if not cls._api_key_is_valid(values.get("api_key")):
            env_key = get_env_api_key(values["provider"])
            if env_key:
                values["api_key"] = env_key
        return values

    @staticmethod
    def _api_key_is_valid(v: Union[str, List[str]]) -> bool:
        """True when ``api_key`` holds at least one usable (non-placeholder) key."""
        keys = v if isinstance(v, list) else [v]
        if not keys:
            return False
        return not any(k in ["", None, "YOUR_API_KEY"] for k in keys)

    @field_validator("api_key")
    @classmethod
    def check_llm_key(cls, v):
        # Non-fatal here: a missing/placeholder key is acceptable when OAuth is
        # configured. The cross-field check in ``check_auth_present`` decides
        # whether to raise, since ``oauth`` isn't visible to a field validator.
        return v

    @model_validator(mode="after")
    def check_auth_present(self):
        """Require either a valid static ``api_key`` or an ``oauth`` config.

        Preserves the legacy behavior (raise ``MissingAPIKeyError``) for the
        non-OAuth path, while allowing an empty/placeholder key when OAuth will
        supply the bearer token.
        """
        if self.oauth is None and not self._api_key_is_valid(self.api_key):
            raise MissingAPIKeyError("Please set your API key in config2.yaml")
        return self

    @field_validator("timeout")
    @classmethod
    def check_timeout(cls, v):
        return v or LLM_API_TIMEOUT


# ---------------------------------------------------------------------------
# Provider catalog: data-only brand presets for LLM configuration.
#
# A *brand* (``provider``) such as ``deepseek`` or ``groq`` is distinct from its
# *wire protocol* (``api_type``): most brands speak the OpenAI-compatible wire
# (``openai_api.py``) at a brand-specific ``base_url``, while ``anthropic`` speaks
# the native Messages API. A :class:`ProviderPreset` captures that mapping plus
# the environment variables that may hold an API key and an optional link to an
# OAuth provider preset.
#
# Co-located with :class:`LLMConfig` (rather than under ``router/``) so the
# ``@model_validator`` above can apply presets without a ``common -> router``
# import cycle. ``router.llm.provider_catalog`` re-exports these names.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProviderPreset:
    """Public, brand-specific defaults applied to an ``LLMConfig``.

    ``base_url`` + ``api_type`` (wire protocol) are filled when the user did not
    set them. ``env_keys`` lists the environment variables searched (in order)
    for an API key. ``oauth_provider`` links to a preset in
    ``OAuthProviderConfig``'s registry when the brand supports OAuth login.
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
