"""Governed activation boundary for the optional Squilla ML backend."""

from __future__ import annotations

import importlib
from dataclasses import dataclass
from typing import Protocol, cast, runtime_checkable

from mote.product.routing.squilla.ml.inference.types import InferenceRequest, InferenceResult


@dataclass(frozen=True, slots=True)
class RoutingBackendManifest:
    identity: str
    provider_kind: str
    module: str
    factory_contract: str
    provenance: str
    capabilities: tuple[str, ...]
    generation: int


SQUILLA_INFERENCE_BACKEND = RoutingBackendManifest(
    identity="mote.routing.squilla.inference-core",
    provider_kind="routing-inference",
    module="mote.product.routing.squilla.ml.inference.core",
    factory_contract="mote.routing.squilla.core-factory.v1",
    provenance="mote-builtin",
    capabilities=("lightgbm", "onnx-mlp", "bge-embedding"),
    generation=1,
)


@runtime_checkable
class RoutingInferenceCore(Protocol):
    def predict(self, request: InferenceRequest) -> InferenceResult: ...


class RoutingInferenceCoreType(Protocol):
    @classmethod
    def from_model_dir(
        cls,
        model_dir: str,
        config: dict,
        *,
        use_aux_head: bool,
    ) -> RoutingInferenceCore: ...


class ApprovedRoutingBackendLoader:
    """Resolve exactly one immutable built-in manifest during activation."""

    def __init__(self, manifest: RoutingBackendManifest = SQUILLA_INFERENCE_BACKEND) -> None:
        if manifest != SQUILLA_INFERENCE_BACKEND:
            raise ValueError("routing backend manifest is not approved")
        self._manifest = manifest

    @property
    def manifest(self) -> RoutingBackendManifest:
        return self._manifest

    def load(self) -> RoutingInferenceCoreType:
        module = importlib.import_module("mote.product.routing.squilla.ml.inference.core")
        candidate = getattr(module, "InferenceCore", None)
        factory = getattr(candidate, "from_model_dir", None)
        if candidate is None or not callable(factory):
            raise TypeError("approved routing backend does not implement the core factory contract")
        return cast(RoutingInferenceCoreType, candidate)


__all__ = [
    "ApprovedRoutingBackendLoader",
    "RoutingBackendManifest",
    "RoutingInferenceCore",
    "RoutingInferenceCoreType",
    "SQUILLA_INFERENCE_BACKEND",
]
