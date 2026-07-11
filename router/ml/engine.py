#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Lazy, graceful-degradation wrapper around the Phase-3 ML inference core.

``SquillaMLEngine`` defers loading :class:`InferenceCore` (and its heavy optional
deps — lightgbm / onnxruntime / sklearn / joblib / tokenizers) until the first
``predict`` call. If the deps OR the cached model bundle are missing, loading
fails once (logged at INFO), ``available`` stays ``False``, and ``predict``
returns ``None`` — letting the caller fall back to the heuristic Gaussian path.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from mote.common.logs import logger
from mote.router.ml.config import default_model_dir, load_runtime_config
from mote.router.ml.inference.core import InferenceCore
from mote.router.ml.inference.types import InferenceRequest, InferenceResult


class SquillaMLEngine:
    """Lazy holder for an :class:`InferenceCore`; degrades to no-op on failure."""

    def __init__(
        self,
        model_dir: str | Path | None = None,
        config: Optional[dict] = None,
        *,
        use_aux_head: Optional[bool] = None,
    ):
        self.model_dir = Path(model_dir) if model_dir else default_model_dir()
        self.config = config if config is not None else load_runtime_config(self.model_dir)
        if use_aux_head is None:
            use_aux_head = bool(self.config.get("v4", {}).get("aux_head_inference", False))
        self.use_aux_head = use_aux_head
        self._core = None
        self._loaded = False  # have we attempted a load yet?
        self._available = False  # did the load succeed?

    @property
    def available(self) -> bool:
        """True once a successful load has happened. Triggers a lazy load."""
        self._ensure_core()
        return self._available

    def _ensure_core(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        if not self.model_dir.is_dir():
            return
        try:
            # Lazy import: pulls lightgbm/onnxruntime/sklearn only on demand.

            self._core = InferenceCore.from_model_dir(
                str(self.model_dir),
                self.config,
                use_aux_head=self.use_aux_head,
            )
            self._available = True
        except Exception as e:  # ImportError, missing files, bad artifacts, ...
            logger.warning(f"router ML engine load failed ({type(e).__name__}: {e}); disabled")
            self._core = None
            self._available = False

    def predict(self, request: InferenceRequest) -> Optional[InferenceResult]:
        """Run ML inference; return ``None`` when the engine is unavailable."""
        self._ensure_core()
        if not self._available or self._core is None:
            return None
        try:
            return self._core.predict(request)
        except Exception as e:
            logger.warning(f"router ML inference failed ({type(e).__name__}: {e}); falling back")
            return None
