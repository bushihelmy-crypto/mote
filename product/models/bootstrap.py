"""Explicit Product integration bootstrap."""

from __future__ import annotations

from pathlib import Path

from mote.contracts.config.model.llm import LLMType
from mote.product.models.providers.anthropic import AnthropicLLM
from mote.product.models.providers.deepseek import DeepSeekLLM
from mote.product.models.providers.openai_chat import OpenAILLM
from mote.product.models.providers.openai_responses import OpenAIResponsesLLM
from mote.product.models.registry import LLMProviderRegistry


def builtin_provider_registry(*, oauth_root: Path | None = None) -> LLMProviderRegistry:
    """Build an isolated catalog containing the Product's bundled providers."""
    registry = LLMProviderRegistry(oauth_root=oauth_root)
    for key in (
        LLMType.OPENAI,
        LLMType.FIREWORKS,
        LLMType.OPEN_LLM,
        LLMType.MOONSHOT,
        LLMType.MISTRAL,
        LLMType.YI,
        LLMType.OPEN_ROUTER,
        LLMType.SILICONFLOW,
    ):
        registry.register(key, OpenAILLM)
    registry.register(LLMType.OPENAI_RESPONSES, OpenAIResponsesLLM)
    registry.register(LLMType.ANTHROPIC, AnthropicLLM)
    registry.register(LLMType.DEEPSEEK, DeepSeekLLM)
    return registry


__all__ = ["builtin_provider_registry"]
