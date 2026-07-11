#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Phase-3 ML routing — vendored opensquilla LightGBM ⊕ MLP inference pipeline.

Only import-safe symbols are exported at package level (the heavy ``InferenceCore``
and its sklearn/lightgbm/onnxruntime deps are reached lazily through
:class:`~mote.router.ml.engine.SquillaMLEngine`).
"""
from mote.router.ml.config import default_model_dir, load_runtime_config
from mote.router.ml.engine import SquillaMLEngine
from mote.router.ml.features import (
    ContextMetadata,
    extract_context_features,
    extract_handcrafted,
    extract_hist_features,
)
from mote.router.ml.inference.postprocess import apply_postprocess
from mote.router.ml.inference.types import FinalDecision, InferenceRequest, InferenceResult
from mote.router.ml.predictor import ROUTE_CLASSES
from mote.router.ml.trajectory import Trajectory, TurnDecision, classify

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
