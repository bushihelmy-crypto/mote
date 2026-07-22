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


# ---------------------------------------------------------------------------
# Process-level shared engine
# ---------------------------------------------------------------------------
# The trained bundle is ~75 MB; a per-role SquillaMLEngine would each lazy-load
# its own copy. Every SquillaStrategy in a process shares one engine per
# resolved ``model_dir`` (memoized below), so the bundle is loaded at most once.
#
# ``InferenceCore.predict`` is stateless after load (all ``self.*`` are read-only
# artifacts; each call builds fresh locals), so under single-loop asyncio the GIL
# serializes calls safely — no lock, no ``to_thread`` (which would reintroduce
# multi-thread reuse risk on that read-only core).
_SHARED: dict[str, SquillaMLEngine] = {}


def shared_engine(model_dir: str | Path | None = None) -> SquillaMLEngine:
    """Return the process-shared engine for *model_dir* (default dir when None).

    Memoized on the resolved directory string so every caller passing the same
    (or default) ``model_dir`` shares one engine — and thus one 75 MB load.
    Distinct directories get distinct engines.
    """
    key = str(Path(model_dir) if model_dir else default_model_dir())
    engine = _SHARED.get(key)
    if engine is None:
        engine = SquillaMLEngine(model_dir=key)
        _SHARED[key] = engine
    return engine


def prewarm(model_dir: str | Path | None = None) -> bool:
    """Eagerly trigger the shared engine's bundle load (process warmup hook).

    Touching ``available`` forces the one-time lazy load off the hot path (e.g.
    at process startup) so the first real ``predict`` doesn't pay the 75 MB
    synchronous load on the event loop. Returns whether the load succeeded;
    a failed/absent bundle is a silent no-op (the heuristic fallback still runs).
    """
    return shared_engine(model_dir).available
