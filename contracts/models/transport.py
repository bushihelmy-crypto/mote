"""Provider-SDK-independent model transport resolution contract."""

from __future__ import annotations

from mote.contracts.config.llm import LLMConfig, LLMType
from mote.contracts.models.capabilities import supports_native_tool_search


def is_anthropic_endpoint(base_url: str) -> bool:
    url = (base_url or "").lower().rstrip("/")
    return "anthropic.com" in url or url.endswith("/anthropic")


def resolve_api_type(config: LLMConfig) -> LLMType:
    """Resolve the endpoint wire protocol without constructing a client."""
    if config.api_type == LLMType.ANTHROPIC or is_anthropic_endpoint(config.base_url or ""):
        return LLMType.ANTHROPIC
    if config.api_type == LLMType.DEEPSEEK or "deepseek" in (config.model or "").lower():
        return LLMType.DEEPSEEK
    if (
        config.api_type == LLMType.OPENAI
        and "openai.com" in (config.base_url or "").lower()
        and supports_native_tool_search(config.model)
    ):
        return LLMType.OPENAI_RESPONSES
    return config.api_type


__all__ = ["is_anthropic_endpoint", "resolve_api_type"]
