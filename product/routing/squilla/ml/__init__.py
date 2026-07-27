#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Phase-3 ML routing — vendored opensquilla LightGBM ⊕ MLP inference pipeline.

Only import-safe symbols are exported at package level (the heavy ``InferenceCore``
and its sklearn/lightgbm/onnxruntime deps are reached lazily through
:class:`~mote.product.routing.squilla.ml.engine.SquillaMLEngine`).
"""
from mote.product.routing.squilla.ml.config import default_model_dir, load_runtime_config
from mote.product.routing.squilla.ml.engine import SquillaMLEngine
from mote.product.routing.squilla.ml.features import (
    ContextMetadata,
    extract_context_features,
    extract_handcrafted,
    extract_hist_features,
)
from mote.product.routing.squilla.ml.inference.postprocess import apply_postprocess
from mote.product.routing.squilla.ml.inference.types import FinalDecision, InferenceRequest, InferenceResult
from mote.product.routing.squilla.ml.predictor import ROUTE_CLASSES
from mote.product.routing.squilla.ml.runtime import RoutingModelActivationError, RoutingModelRuntime
from mote.product.routing.squilla.ml.trajectory import Trajectory, TurnDecision, classify

__all__ = [
    "SquillaMLEngine",
    "RoutingModelActivationError",
    "RoutingModelRuntime",
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
