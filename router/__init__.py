#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Router package — LLM routing system (replaces the old LLM factory)."""
from metagpt.router.complexity import (
    ComplexitySignals,
    ContextSignals,
    RoutingRule,
    complexity_score,
    decide_tier,
    extract_all_signals,
    score_to_tier,
    signals_from_messages,
)
from metagpt.router.control import (
    RouterControlHold,
    RouterControlHoldStore,
    RouterControlTarget,
)
from metagpt.router.flags import RoutingFlags, compute_flags
from metagpt.router.llm.base_llm import BaseLLM
from metagpt.router.ml import (
    SquillaMLEngine,
    apply_postprocess,
    default_model_dir,
    load_runtime_config,
)
from metagpt.router.ml.predictor import ROUTE_CLASSES
from metagpt.router.router import LLM, LLMRouter, get_router
from metagpt.router.schema import ModelCard, RoutingDecision, RoutingRequest
from metagpt.router.squilla import (
    RoutingHistoryStore,
    SquillaConfig,
    SquillaStrategy,
    detect_complaint,
    score_to_probs,
)
from metagpt.router.strategy import (
    ComplexityStrategy,
    LLMJudgeStrategy,
    RoutingStrategy,
    RuleBasedStrategy,
)

__all__ = [
    "LLM",
    "LLMRouter",
    "get_router",
    "RoutingRequest",
    "RoutingDecision",
    "ModelCard",
    "RoutingStrategy",
    "RuleBasedStrategy",
    "ComplexityStrategy",
    "LLMJudgeStrategy",
    "BaseLLM",
    # full opensquilla routing port
    "SquillaStrategy",
    "SquillaConfig",
    "RoutingHistoryStore",
    "detect_complaint",
    "score_to_probs",
    # ML inference pipeline (LightGBM ⊕ MLP, graceful fallback)
    "SquillaMLEngine",
    "apply_postprocess",
    "default_model_dir",
    "load_runtime_config",
    "ROUTE_CLASSES",
    "RoutingFlags",
    "compute_flags",
    "RouterControlHold",
    "RouterControlHoldStore",
    "RouterControlTarget",
    # complexity analysis primitives
    "decide_tier",
    "complexity_score",
    "score_to_tier",
    "extract_all_signals",
    "signals_from_messages",
    "ComplexitySignals",
    "ContextSignals",
    "RoutingRule",
]
