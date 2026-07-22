#!/usr/bin/env python
# -*- coding: utf-8 -*-
from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field

from mote.common.config.config.llm_config import LLMConfig, LLMType
from mote.common.config.loader import load_config
from mote.common.config.meta_config import Config
from mote.router.cost import CostTracker, PricingMode
from mote.router.llm.base_llm import BaseLLM
from mote.router.llm.llm_provider_registry import create_llm_instance, resolve_api_type
from mote.router.ratelimit import RateLimitTracker


class Context(BaseModel):
    """LLM build context for Mote.

    Bundles the global :class:`Config` with a :class:`CostTracker` and exposes
    the only capability the router needs: build a :class:`BaseLLM` from the
    default config (``llm``) or an explicit ``LLMConfig``, wiring the right cost
    tracker in each case.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    config: Config = Field(default_factory=load_config)
    cost_manager: CostTracker = Field(default_factory=CostTracker)
    # Fleet-wide rate-limit quota (provider account state, last-write-wins per
    # endpoint) — the ``/usage`` limit side, counterpart to ``cost_manager``.
    # Shared by every LLM this Context builds; each provider's response hook
    # observes into it. See :mod:`mote.router.ratelimit`.
    rate_limit_tracker: RateLimitTracker = Field(default_factory=RateLimitTracker)
    # Explicit reference to the live control plane (an ``AgentControl``), when a
    # caller that holds this Context knows its plane. Duck-typed to ``Any`` to
    # keep the router layer free of any ``environment`` import; ``resolve_control``
    # reads it first, then falls back to the ambient ``current_control()``.
    agent_control: Optional[Any] = Field(default=None, exclude=True)

    def _select_cost_manager(self, llm_config: LLMConfig) -> CostTracker:
        """Return a CostTracker whose pricing mode matches the config's api_type.

        Self-hosted open models cost nothing (FREE), Fireworks bills by model
        size (FIREWORKS), everything else uses the standard cache-aware table
        and shares the session-wide tracker so per-model totals roll up.

        Keys off the *resolved* api_type (same source the provider client is
        chosen from) so pricing mode and provider can never disagree — e.g. a
        Claude-via-anthropic.com config never bills on the Fireworks table.
        """
        api_type = resolve_api_type(llm_config)
        if api_type == LLMType.FIREWORKS:
            return CostTracker(mode=PricingMode.FIREWORKS)
        elif api_type == LLMType.OPEN_LLM:
            return CostTracker(mode=PricingMode.FREE)
        else:
            return self.cost_manager

    def llm(self) -> BaseLLM:
        """Build a BaseLLM for the default (``config.models.default``) model."""
        default = self.config.models.default
        llm = create_llm_instance(default)
        if llm.cost_manager is None:
            llm.cost_manager = self._select_cost_manager(default)
        llm.rate_limit_tracker = self.rate_limit_tracker
        return llm

    def llm_with_cost_manager_from_llm_config(self, llm_config: LLMConfig) -> BaseLLM:
        """Build a BaseLLM from a given ``LLMConfig``."""
        llm = create_llm_instance(llm_config)
        if llm.cost_manager is None:
            llm.cost_manager = self._select_cost_manager(llm_config)
        llm.rate_limit_tracker = self.rate_limit_tracker
        return llm
