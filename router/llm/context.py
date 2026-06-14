#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@Time    : 2024/1/4 16:32
@Author  : alexanderwu
@File    : context.py
"""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from metagpt.common.config2 import Config
from metagpt.common.config.llm_config import LLMConfig, LLMType
from metagpt.common.utils.cost_manager import (
    CostManager,
    FireworksCostManager,
    TokenCostManager,
)
from metagpt.router.llm.base_llm import BaseLLM
from metagpt.router.llm.llm_provider_registry import create_llm_instance


class Context(BaseModel):
    """LLM build context for MetaGPT.

    Bundles the global :class:`Config` with a :class:`CostManager` and exposes
    the only capability the router needs: build a :class:`BaseLLM` from the
    default config (``llm``) or an explicit ``LLMConfig``, wiring the right cost
    manager in each case.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    config: Config = Field(default_factory=Config.default)
    cost_manager: CostManager = CostManager()

    def _select_costmanager(self, llm_config: LLMConfig) -> CostManager:
        """Return a CostManager instance matching the config's api_type."""
        if llm_config.api_type == LLMType.FIREWORKS:
            return FireworksCostManager()
        elif llm_config.api_type == LLMType.OPEN_LLM:
            return TokenCostManager()
        else:
            return self.cost_manager

    def llm(self) -> BaseLLM:
        """Build a BaseLLM for the default (``config.llm``) model."""
        llm = create_llm_instance(self.config.llm)
        if llm.cost_manager is None:
            llm.cost_manager = self._select_costmanager(self.config.llm)
        return llm

    def llm_with_cost_manager_from_llm_config(self, llm_config: LLMConfig) -> BaseLLM:
        """Build a BaseLLM from a given ``LLMConfig``."""
        llm = create_llm_instance(llm_config)
        if llm.cost_manager is None:
            llm.cost_manager = self._select_costmanager(llm_config)
        return llm
