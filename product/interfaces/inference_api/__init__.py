"""Versioned HTTP compatibility adapter for the existing model gateway."""

from mote.product.inference.session_gateway import RealtimeSession, RealtimeSessionOwner, RuntimeSessionGateway
from mote.product.interfaces.inference_api.application import (
    DurableResponseOwner,
    InferenceApiAuthorizer,
    build_inference_api,
)
from mote.product.interfaces.inference_api.composition import InferenceRuntimeLease, build_generation_inference_api
from mote.product.interfaces.inference_api.model_operations import ModelGatewayCompatibilityOwner
from mote.product.interfaces.inference_api.operations import (
    ArtifactCompatibilityOwner,
    DurableCompatibilityOwner,
    UnaryCompatibilityOwner,
)
from mote.product.interfaces.inference_api.runtime_operations import (
    ArtifactTransferCompatibilityOwner,
    CommandCompatibilityOwner,
    ResponseCompatibilityOwner,
)

__all__ = [
    "DurableResponseOwner",
    "ArtifactCompatibilityOwner",
    "ArtifactTransferCompatibilityOwner",
    "CommandCompatibilityOwner",
    "ResponseCompatibilityOwner",
    "DurableCompatibilityOwner",
    "InferenceApiAuthorizer",
    "InferenceRuntimeLease",
    "ModelGatewayCompatibilityOwner",
    "RealtimeSession",
    "RealtimeSessionOwner",
    "RuntimeSessionGateway",
    "UnaryCompatibilityOwner",
    "build_inference_api",
    "build_generation_inference_api",
]
