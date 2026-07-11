#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Phase-3 ML inference subpackage (LightGBM ⊕ MLP ensemble).

Only the import-safe dataclasses are re-exported here. :class:`InferenceCore`
is intentionally NOT imported eagerly — it transitively pulls heavy optional
deps (sklearn / joblib via ``v4_features``). Import it lazily inside the guarded
engine load (``mote.router.ml.engine``):

    from mote.router.ml.inference.core import InferenceCore
"""
from mote.router.ml.inference.types import FeatureBundle, FinalDecision, HeadOutputs, InferenceRequest, InferenceResult

__all__ = [
    "FeatureBundle",
    "FinalDecision",
    "HeadOutputs",
    "InferenceRequest",
    "InferenceResult",
]
