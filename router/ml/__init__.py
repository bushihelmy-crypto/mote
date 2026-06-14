#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Phase-3 ML routing — vendored opensquilla LightGBM ⊕ MLP inference pipeline.

Only import-safe symbols are exported at package level (the heavy ``InferenceCore``
and its sklearn/lightgbm/onnxruntime deps are reached lazily through
:class:`~metagpt.router.ml.engine.SquillaMLEngine`).
"""
from metagpt.router.ml.config import default_model_dir, load_runtime_config
from metagpt.router.ml.engine import SquillaMLEngine
from metagpt.router.ml.features import (
    ContextMetadata,
    extract_context_features,
    extract_handcrafted,
    extract_hist_features,
)
from metagpt.router.ml.inference.postprocess import apply_postprocess
from metagpt.router.ml.inference.types import (
    FinalDecision,
    InferenceRequest,
    InferenceResult,
)
from metagpt.router.ml.predictor import ROUTE_CLASSES
from metagpt.router.ml.trajectory import Trajectory, TurnDecision, classify

__all__ = [
    "SquillaMLEngine",
    "InferenceRequest",
    "InferenceResult",
    "FinalDecision",
    "apply_postprocess",
    "ROUTE_CLASSES",
    "ContextMetadata",
    "extract_handcrafted",
    "extract_context_features",
    "extract_hist_features",
    "Trajectory",
    "TurnDecision",
    "classify",
    "load_runtime_config",
    "default_model_dir",
]
