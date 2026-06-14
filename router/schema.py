#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Pure data structures for the LLM router.

These carry no behavior beyond simple derivations; all routing logic lives in
``strategy.py`` (intelligent routing) and ``router.py`` (the unified entry).
"""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field

from metagpt.common.config.llm_config import LLMConfig
from metagpt.common.const.llm import MULTI_MODAL_MODELS
from metagpt.common.utils.token_counter import count_message_tokens, count_string_tokens


class ModelCard(BaseModel):
    """Metadata describing a routable named model.

    Inspired by opensquilla's ModelCard: provider/model capability metadata plus a
    coarse ``tier`` cost/strength rank used by the rule-based strategy.
    """

    model_config = {"arbitrary_types_allowed": True}

    name: str
    llm_config: LLMConfig
    description: str = ""
    tags: set[str] = Field(default_factory=set)
    # 0 = cheap/fast ... 3 = strongest/most expensive
    tier: int = 1
    context_window: Optional[int] = None

    @property
    def supports_vision(self) -> bool:
        """Whether the underlying model accepts image input.

        Derived from ``llm_config.model`` against ``MULTI_MODAL_MODELS``
        (mirrors ``BaseLLM.support_image_input``).
        """
        model = self.llm_config.model or ""
        return any(m in model for m in MULTI_MODAL_MODELS)


class RoutingRequest(BaseModel):
    """Signals describing a single routing request (intelligent routing input)."""

    text: str = ""
    messages: Optional[list[dict]] = None
    estimated_tokens: Optional[int] = None
    requires_vision: bool = False
    requires_pdf: bool = False
    prefer_cheap: bool = False
    # free-form signal flags: high_risk / debug / long_context / strict_format
    flags: set[str] = Field(default_factory=set)
    # session identity for stateful strategies (routing history / control holds)
    session_key: str = "default"

    def token_estimate(self, model: str = "gpt-3.5-turbo-0125") -> int:
        """Best-effort token count: explicit value wins, else count messages/text."""
        if self.estimated_tokens is not None:
            return self.estimated_tokens
        if self.messages:
            try:
                return count_message_tokens(self.messages, model)
            except Exception:
                pass
        if self.text:
            try:
                return count_string_tokens(self.text, model)
            except Exception:
                pass
        return 0

    def prompt_text(self) -> str:
        """Flatten the request to a single analysis string.

        The full conversation (``messages``) is the primary input; ``text`` is
        the single-utterance special case used when no messages are supplied.
        Mirrors ``token_estimate``'s messages-before-text precedence.
        """
        if self.messages:
            parts = [
                str(m.get("content", ""))
                for m in self.messages
                if isinstance(m, dict) and m.get("content")
            ]
            if parts:
                return "\n".join(parts)
        return self.text


class RoutingDecision(BaseModel):
    """The outcome of a routing decision."""

    name: str
    confidence: float = 1.0
    source: Literal["explicit", "task", "rule", "complexity", "llm_judge", "squilla"] = "rule"
    fallback: bool = False
    # human-readable reasons behind the decision (mainly complexity/rule routing)
    reasons: list[str] = Field(default_factory=list)
    tier: Optional[str] = None  # LOW / MEDIUM / HIGH (complexity) or R0-R3 (squilla)
    # strategy-specific metadata (squilla: thinking_mode / prompt_policy / flags / ...)
    extra: dict = Field(default_factory=dict)
