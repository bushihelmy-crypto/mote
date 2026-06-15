#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@Time    : 2024/1/4 16:33
@Author  : alexanderwu
@File    : llm_config.py
"""
from enum import Enum
from typing import List, Optional, Union

from pydantic import field_validator, model_validator

from metagpt.common.config.config.compress_msg_config import CompressType
from metagpt.common.config.config.oauth_config import OAuthProviderConfig
from metagpt.common.const import LLM_API_TIMEOUT
from metagpt.common.exception import MissingAPIKeyError
from metagpt.common.utils.yaml_model import YamlModel


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

    # Compress request messages under token limit
    compress_type: CompressType = CompressType.NO_COMPRESS

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
