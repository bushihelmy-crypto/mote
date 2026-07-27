#!/usr/bin/env python
# -*- coding: utf-8 -*-
import os
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Literal, Mapping, Optional, Tuple, Union
from urllib.parse import urlparse

from pydantic import field_validator, model_validator

from mote.contracts.config.base import ConfigModel as YamlModel
from mote.contracts.config.oauth import OAuthProviderConfig
from mote.contracts.errors.config import MissingAPIKeyError
from mote.contracts.models.constants import LLM_API_TIMEOUT


class LLMType(Enum):
    """LLM types.

    All values except ``ANTHROPIC`` use the OpenAI-compatible client
    (``openai_api.py`` with a provider-specific ``base_url``). ``ANTHROPIC``
    selects the native Anthropic Messages API client (``anthropic_api.py``);
    it is also auto-detected when ``base_url`` points at ``anthropic.com``.
    """

    OPENAI = "openai"
    # OpenAI Responses API transport (openai_responses_api.py, responses.create).
    # A whole-model takeover for gpt-5.4+ (native tool_search), resolved
    # dynamically by resolve_api_type — never a brand preset (transport, not a
    # brand). See supports_native_tool_search.
    OPENAI_RESPONSES = "openai_responses"
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
    #
    # The sentinel ``"auto"`` asks mote to infer the brand at load time from the
    # strongest available signal — an explicit ``base_url`` host, then a ``model``
    # name hint, then a brand API key present in the environment (see
    # :func:`detect_provider`). When nothing is recognisable it falls back to the
    # plain default path (as if ``provider`` were unset).
    provider: Optional[str] = None

    # A single key, or a list of keys to rotate through on auth/billing failures
    # (recovery=ROTATE_CREDENTIAL). The first key is used until one is exhausted.
    api_key: Union[str, List[str]] = "sk-"
    api_type: LLMType = LLMType.OPENAI
    base_url: str = "https://api.openai.com/v1"

    # Opt-in OAuth: when set, the bearer token is obtained/refreshed from an
    # OAuth token endpoint instead of using the static ``api_key``. Leaving this
    # ``None`` preserves the static-key path unchanged.
    oauth: Optional[OAuthProviderConfig] = None

    model: Optional[str] = None  # also stands for DEPLOYMENT_NAME
    pricing_plan: Optional[str] = None  # Cost Settlement Plan Parameters.

    # For Chat Completion. Only the two knobs the provider clients actually send
    # on the wire (``max_tokens`` + ``temperature``); the remaining OpenAI
    # sampling params were never plumbed through ``_cons_kwargs`` and are omitted.
    max_token: int = 4096
    temperature: float = 0.0
    timeout: int = 600

    # Unified reasoning / thinking effort. ``None`` (default) leaves thinking OFF.
    # Each provider translates this single enum into its own wire shape, gated by
    # ``ModelProfile.supports_thinking`` (an incapable model silently ignores it):
    # Anthropic → ``thinking={"type":"enabled","budget_tokens":...}`` (temperature
    # dropped, as the API requires); OpenAI Responses → ``reasoning={"effort":...}``;
    # OpenAI Chat Completions → ``reasoning_effort=...``.
    reasoning_effort: Optional[Literal["minimal", "low", "medium", "high"]] = None

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
        if str(values["provider"]).strip().lower() == "auto":
            detected = detect_provider(values)
            if not detected:
                # Nothing recognisable — behave as if ``provider`` were unset so
                # the plain default path (and any explicit fields) stay intact.
                values["provider"] = None
                return values
            values["provider"] = detected
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

    A preset is the SINGLE SOURCE OF TRUTH for one brand's identity: its config
    (``base_url`` + ``api_type`` wire protocol, filled when the user left them
    empty), the environment variables searched (in order) for an API key
    (``env_keys``), an optional link to an OAuth login preset (``oauth_provider``),
    and — crucially — the two ways a user or a model name may NAME this brand:

    * ``aliases`` — extra human spellings accepted for an explicit ``provider:``
      (e.g. ``zhipu``/``bigmodel`` → the ``zhipuai`` preset). The canonical
      catalog key is always accepted; aliases are the additional nicknames.
    * ``model_markers`` — lowercase model-name substrings that let ``provider:
      auto`` infer this brand from the model name alone (e.g. ``glm`` → zhipuai).

    INVARIANT (enforced at import by :func:`_build_alias_index`): every
    ``model_marker`` is ALSO a valid ``provider:`` name for the same brand — it is
    registered as an alias too. So a brand's complete set of accepted names is
    ``{canonical} ∪ aliases ∪ model_markers``, and ``provider: auto`` and an
    explicit ``provider:`` can never disagree about what a name resolves to.
    Aliases/markers must be globally unique across brands (collisions raise at
    import). This keeps brand identity in ONE table — no second lookup map to
    drift against.
    """

    base_url: str
    api_type: LLMType
    env_keys: List[str] = field(default_factory=list)
    default_model: Optional[str] = None
    oauth_provider: Optional[str] = None
    aliases: Tuple[str, ...] = ()
    model_markers: Tuple[str, ...] = ()


# brand name -> preset. Most brands use the OpenAI-compatible wire with a
# brand-specific base_url; brands that already have a dedicated LLMType keep it
# (so cost-manager selection is unchanged); anthropic uses the native wire.
PROVIDER_CATALOG: Dict[str, ProviderPreset] = {
    "openai": ProviderPreset(
        base_url="https://api.openai.com/v1",
        api_type=LLMType.OPENAI,
        env_keys=["OPENAI_API_KEY"],
        oauth_provider="openai",
        model_markers=("gpt",),
    ),
    "anthropic": ProviderPreset(
        base_url="https://api.anthropic.com",
        api_type=LLMType.ANTHROPIC,
        env_keys=["ANTHROPIC_API_KEY"],
        oauth_provider="anthropic",
        model_markers=("claude",),
    ),
    "deepseek": ProviderPreset(
        base_url="https://api.deepseek.com/v1",
        api_type=LLMType.DEEPSEEK,
        env_keys=["DEEPSEEK_API_KEY"],
        model_markers=("deepseek",),
    ),
    "moonshot": ProviderPreset(
        base_url="https://api.moonshot.cn/v1",
        api_type=LLMType.MOONSHOT,
        env_keys=["MOONSHOT_API_KEY"],
        aliases=("kimi",),
        model_markers=("moonshot", "kimi"),
    ),
    "mistral": ProviderPreset(
        base_url="https://api.mistral.ai/v1",
        api_type=LLMType.MISTRAL,
        env_keys=["MISTRAL_API_KEY"],
        model_markers=("mistral",),
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
        aliases=("grok",),
        model_markers=("grok",),
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
        model_markers=("minimax",),
    ),
    "dashscope": ProviderPreset(
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        api_type=LLMType.OPENAI,
        env_keys=["DASHSCOPE_API_KEY", "ALIBABA_API_KEY"],
        aliases=("qwen", "tongyi", "aliyun", "alibaba"),
        model_markers=("qwen",),
    ),
    "zhipuai": ProviderPreset(
        base_url="https://open.bigmodel.cn/api/paas/v4",
        api_type=LLMType.OPENAI,
        env_keys=["ZHIPUAI_API_KEY"],
        aliases=("zhipu", "glm", "bigmodel"),
        model_markers=("glm",),
    ),
    "stepfun": ProviderPreset(
        base_url="https://api.stepfun.com/v1",
        api_type=LLMType.OPENAI,
        env_keys=["STEPFUN_API_KEY"],
    ),
    "hunyuan": ProviderPreset(
        # Tencent Hunyuan OpenAI-compatible surface (also has a /anthropic one).
        base_url="https://api.hunyuan.cloud.tencent.com/v1",
        api_type=LLMType.OPENAI,
        env_keys=["HUNYUAN_API_KEY"],
        model_markers=("hunyuan",),
    ),
    "xiaomi": ProviderPreset(
        # Xiaomi MiMo OpenAI-compatible surface (also has a /anthropic one).
        base_url="https://api.xiaomimimo.com/v1",
        api_type=LLMType.OPENAI,
        env_keys=["XIAOMI_API_KEY", "MIMO_API_KEY"],
        aliases=("mimo",),
        model_markers=("mimo",),
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
    # --- OpenAI-compatible brands mirrored from the pydantic-ai catalog. Each
    # speaks the OpenAI wire at a brand-specific base_url, so it needs only a
    # preset here (no dedicated LLMType / transport). Brands that require a
    # different wire (Azure per-deployment endpoints, AWS Bedrock SigV4, Google
    # Vertex ADC) are intentionally NOT faked as base_url presets.
    "google": ProviderPreset(
        # Gemini via its OpenAI-compatible surface (the native GenAI/Vertex SDK
        # is a separate transport, not wired here).
        base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
        api_type=LLMType.OPENAI,
        env_keys=["GEMINI_API_KEY", "GOOGLE_API_KEY"],
        aliases=("gemini",),
        model_markers=("gemini",),
    ),
    "cohere": ProviderPreset(
        # Cohere Command models via their OpenAI-compatibility endpoint.
        base_url="https://api.cohere.ai/compatibility/v1",
        api_type=LLMType.OPENAI,
        env_keys=["CO_API_KEY", "COHERE_API_KEY"],
    ),
    "huggingface": ProviderPreset(
        base_url="https://router.huggingface.co/v1",
        api_type=LLMType.OPENAI,
        env_keys=["HF_TOKEN", "HUGGINGFACE_API_KEY"],
        aliases=("hf",),
    ),
    "github": ProviderPreset(
        # GitHub Models — distinct from the github-copilot coding endpoint above.
        base_url="https://models.github.ai/inference",
        api_type=LLMType.OPENAI,
        env_keys=["GITHUB_API_KEY"],
        aliases=("github-models",),
    ),
    "heroku": ProviderPreset(
        base_url="https://us.inference.heroku.com/v1",
        api_type=LLMType.OPENAI,
        env_keys=["HEROKU_INFERENCE_KEY"],
    ),
    "nebius": ProviderPreset(
        base_url="https://api.studio.nebius.com/v1",
        api_type=LLMType.OPENAI,
        env_keys=["NEBIUS_API_KEY"],
    ),
    "ollama": ProviderPreset(
        # Local Ollama server (override base_url for a remote instance).
        base_url="http://localhost:11434/v1",
        api_type=LLMType.OPENAI,
        env_keys=["OLLAMA_API_KEY"],
    ),
    "ovhcloud": ProviderPreset(
        base_url="https://oai.endpoints.kepler.ai.cloud.ovh.net/v1",
        api_type=LLMType.OPENAI,
        env_keys=["OVHCLOUD_API_KEY"],
        aliases=("ovh",),
    ),
    "sambanova": ProviderPreset(
        base_url="https://api.sambanova.ai/v1",
        api_type=LLMType.OPENAI,
        env_keys=["SAMBANOVA_API_KEY"],
    ),
    "vercel": ProviderPreset(
        base_url="https://ai-gateway.vercel.sh/v1",
        api_type=LLMType.OPENAI,
        env_keys=["VERCEL_AI_GATEWAY_API_KEY", "VERCEL_OIDC_TOKEN"],
    ),
    "litellm": ProviderPreset(
        # Self-hosted LiteLLM proxy; override base_url to point at your instance.
        base_url="http://localhost:4000",
        api_type=LLMType.OPENAI,
        env_keys=["LITELLM_API_KEY", "LITELLM_MASTER_KEY"],
    ),
}


def _build_alias_index(catalog: Mapping[str, ProviderPreset]) -> Dict[str, str]:
    """Build the accepted-name → canonical-brand reverse index (fail-fast).

    A brand's accepted names are its canonical catalog key plus every
    ``alias`` and every ``model_marker`` (markers are registered as aliases too,
    so ``provider: auto`` and an explicit ``provider:`` resolve identically —
    the class invariant documented on :class:`ProviderPreset`). All names are
    lowercased. A name claimed by two different brands raises ``ValueError`` at
    import time — brand identity stays unique and drift is impossible.
    """
    index: Dict[str, str] = {}

    def _claim(name: str, canonical: str) -> None:
        key = name.strip().lower()
        if not key:
            return
        owner = index.get(key)
        if owner is not None and owner != canonical:
            raise ValueError(
                f"provider name {key!r} claimed by both {owner!r} and {canonical!r}; "
                "aliases and model_markers must be globally unique"
            )
        index[key] = canonical

    for canonical, preset in catalog.items():
        _claim(canonical, canonical)
        for alias in preset.aliases:
            _claim(alias, canonical)
        # Every model_marker is also a valid explicit provider name (invariant).
        for marker in preset.model_markers:
            _claim(marker, canonical)
    return index


# Accepted-name → canonical-brand, computed once at import (fail-fast on any
# cross-brand name collision). The single reverse index behind BOTH explicit
# ``provider:`` resolution (get_provider_preset) and ``provider: auto`` model
# inference (detect_provider) — there is no second lookup table to drift.
_ALIAS_INDEX: Dict[str, str] = _build_alias_index(PROVIDER_CATALOG)


def list_providers() -> List[str]:
    """Return the registered provider brand names (sorted)."""
    return sorted(PROVIDER_CATALOG)


def resolve_provider_name(name: str) -> Optional[str]:
    """Return the canonical brand for a user-supplied ``provider:`` name.

    Accepts the canonical key or any registered alias / model-marker
    (case-insensitive, whitespace-trimmed). ``None`` when unrecognised.
    """
    return _ALIAS_INDEX.get((name or "").strip().lower())


def get_provider_preset(name: str) -> ProviderPreset:
    """Return the :class:`ProviderPreset` for ``name``.

    Resolves the canonical brand via the alias index, so a nickname or model
    marker (e.g. ``glm``/``zhipu`` → ``zhipuai``, ``kimi`` → ``moonshot``) works
    exactly like the canonical key. Raises ``KeyError`` (listing the known
    providers) when unknown. Matching is case-insensitive and whitespace-trimmed.
    """
    canonical = resolve_provider_name(name)
    if canonical is None:
        raise KeyError(f"unknown provider {name!r}; known: {list_providers()}")
    return PROVIDER_CATALOG[canonical]


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
    # Normalise a nickname/model-marker to the canonical brand so downstream
    # (env-key lookup, cost selection) always sees one stable name.
    canonical = resolve_provider_name(provider)
    if canonical is not None:
        values["provider"] = canonical
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


def _url_host(url: str) -> str:
    """Return the lowercased hostname of ``url`` (empty when unparseable)."""
    try:
        return (urlparse(url).hostname or "").lower()
    except ValueError:
        return ""


def detect_provider(values: Mapping[str, Any], environ: Optional[Mapping[str, str]] = None) -> Optional[str]:
    """Infer a concrete brand for ``provider: auto`` from base_url/model/env.

    Priority (most explicit first): an explicit ``base_url`` whose host matches a
    catalog preset (exact or subdomain), then a ``model`` name hint, then the
    first brand whose API-key env var is set. Returns a brand name in
    :data:`PROVIDER_CATALOG`, or ``None`` when nothing is recognisable (the caller
    then keeps the plain default path).
    """
    environ = os.environ if environ is None else environ

    host = _url_host(values.get("base_url") or "")
    if host:
        for name, preset in PROVIDER_CATALOG.items():
            preset_host = _url_host(preset.base_url)
            if preset_host and (host == preset_host or host.endswith("." + preset_host)):
                return name

    model = (values.get("model") or "").lower()
    if model:
        for name, preset in PROVIDER_CATALOG.items():
            if any(marker in model for marker in preset.model_markers):
                return name

    for name, preset in PROVIDER_CATALOG.items():
        if any(environ.get(k) for k in preset.env_keys):
            return name

    return None


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
