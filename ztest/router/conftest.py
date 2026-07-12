#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Shared fixtures for the router test suite.

Key facts the fixtures encode:

- ``ModelCard.supports_vision`` is a *substring* match against
  ``MULTI_MODAL_MODELS`` (``['gpt-4o','gpt-4o-mini','sonnet','opus','gemini','gpt']``),
  so ANY model whose name contains ``gpt`` counts as vision-capable. Non-vision
  test cards therefore use ``deepseek-chat`` / ``qwen-max`` / ``claude-3-haiku``,
  and the single vision card uses ``gpt-4o``.
- ``LLMRouter._build`` constructs real provider instances via
  ``context.llm`` / ``context.llm_with_cost_manager_from_llm_config``; the
  ``router`` fixture stubs both with a :class:`FakeLLM` so no network/provider is
  touched.
"""
from __future__ import annotations

import pytest

from mote.common.config.config.llm_config import LLMConfig
from mote.router.schema import ModelCard


class FakeLLM:
    """Minimal duck-typed stand-in for a BaseLLM used by router/strategy tests."""

    def __init__(self, name: str = "fake", reply: str = ""):
        self.name = name
        self.reply = reply
        self.aask_calls: list[str] = []
        # Mirror the attribute the router wires onto every built instance.
        self._fallback_supplier = None

    async def aask(self, prompt, stream=True, **kwargs):  # noqa: D401
        self.aask_calls.append(prompt)
        return self.reply


class _FakeContext:
    """Stand-in for the pydantic ``Context`` LLM factory.

    The real ``Context`` is a pydantic model that rejects monkeypatching its
    ``llm`` method (no such field), so the ``router`` fixture swaps the whole
    context object with this duck-typed builder that returns :class:`FakeLLM`.
    """

    def llm(self) -> "FakeLLM":
        return FakeLLM(name="llm")

    def llm_with_cost_manager_from_llm_config(self, llm_config) -> "FakeLLM":
        return FakeLLM(name=getattr(llm_config, "model", "cfg"))


def make_card(
    name: str,
    *,
    model: str = "gpt-4o",
    tier: int = 1,
    tags=None,
    context_window=None,
    description: str = "",
) -> ModelCard:
    """Build a ModelCard with a throwaway LLMConfig (api_key never used in tests)."""
    return ModelCard(
        name=name,
        llm_config=LLMConfig(api_key="sk-test", model=model),
        description=description,
        tags=set(tags or []),
        tier=tier,
        context_window=context_window,
    )


@pytest.fixture
def cards() -> dict[str, ModelCard]:
    """A 4-card tier ladder. Only ``vision`` (gpt-4o) supports vision."""
    return {
        "cheap": make_card("cheap", model="deepseek-chat", tier=0, context_window=8_000),
        "mid": make_card("mid", model="qwen-max", tier=1, context_window=32_000),
        "vision": make_card("vision", model="gpt-4o", tier=2, context_window=128_000),
        "strong": make_card("strong", model="claude-3-haiku", tier=3, context_window=200_000),
    }


@pytest.fixture
def router(cards):
    """An LLMRouter whose provider construction is stubbed to return FakeLLMs.

    The router's auto-registered config cards are cleared and replaced with the
    deterministic ``cards`` ladder, so routing assertions don't depend on the
    machine's mote config. ``context`` is swapped for a duck-typed fake so no
    real provider is constructed.
    """
    from mote.router.router import LLMRouter

    r = LLMRouter()
    # Replace auto-registered cards with our deterministic ladder + a default.
    r._cards = dict(cards)
    r._cards["llm"] = make_card("llm", model="claude-3-haiku", tier=1)
    r._instances.clear()
    r._default = "llm"
    r.context = _FakeContext()
    return r
