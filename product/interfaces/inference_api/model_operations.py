"""Canonical finite-operation projection onto the single ModelGateway."""

from __future__ import annotations

from dataclasses import asdict
from typing import cast
from uuid import uuid4

from pydantic import JsonValue

from mote.contracts.artifact import ArtifactRef, ArtifactRetention, ArtifactSensitivity
from mote.contracts.model.invocation import (
    EmbeddingInput,
    EmbeddingOutput,
    ImageGenerationInput,
    ImageGenerationOutput,
    ModelInvocation,
    SpeechInput,
    SpeechOutput,
    TranscriptionInput,
    TranscriptionOutput,
)
from mote.contracts.model.operations import ModelOperation
from mote.contracts.model.topology import DefaultRoute, RouteId
from mote.contracts.ports.model.gateway import ModelGateway
from mote.product.interfaces.inference_api.operations import UnaryCompatibilityOwner


class ModelGatewayCompatibilityOwner(UnaryCompatibilityOwner):
    def __init__(self, gateway: ModelGateway, *, route_id: RouteId | None = None) -> None:
        self._gateway = gateway
        self._route_id = route_id or DefaultRoute()

    async def execute(self, operation: str, payload: dict[str, JsonValue]) -> dict[str, JsonValue]:
        invocation = _invocation(operation, payload, self._route_id)
        result = await self._gateway.execute(invocation, stream=False)
        output = result.output
        if output.kind != invocation.operation.value:
            raise RuntimeError("model gateway returned an incompatible operation")
        document = _response(output)
        document["model"] = result.model_or_deployment
        document["usage"] = cast(
            JsonValue,
            {
                "input_tokens": result.usage.input_tokens,
                "output_tokens": result.usage.output_tokens,
                "total_tokens": result.usage.total_tokens,
            },
        )
        return document


def _invocation(operation: str, payload: dict[str, JsonValue], route_id: RouteId) -> ModelInvocation:
    call_id = str(payload.get("request_id") or uuid4())
    if operation == "embeddings.create":
        raw = payload.get("input")
        if isinstance(raw, str):
            values = (raw,)
        elif isinstance(raw, list) and raw and all(isinstance(item, str) for item in raw):
            values = tuple(cast(list[str], raw))
        else:
            raise ValueError("embedding input must be a string or non-empty string array")
        dimensions = payload.get("dimensions")
        if dimensions is not None and not isinstance(dimensions, int):
            raise ValueError("embedding dimensions must be an integer")
        model_input = EmbeddingInput(values=values, dimensions=dimensions)
        model_operation = ModelOperation.EMBEDDING
    elif operation == "images.generate":
        prompt = payload.get("prompt")
        if not isinstance(prompt, str) or not prompt:
            raise ValueError("image prompt is required")
        model_input = ImageGenerationInput(prompt=prompt, options=_options(payload, {"prompt", "request_id"}))
        model_operation = ModelOperation.IMAGE_GENERATION
    elif operation == "audio.speech":
        text, voice = payload.get("input"), payload.get("voice")
        if not isinstance(text, str) or not text:
            raise ValueError("speech input is required")
        if not isinstance(voice, str) or not voice:
            raise ValueError("speech voice is required")
        model_input = SpeechInput(
            text=text,
            voice=voice,
            options=_options(payload, {"input", "voice", "request_id"}),
        )
        model_operation = ModelOperation.SPEECH
    elif operation == "audio.transcriptions":
        artifact = payload.get("artifact")
        if not isinstance(artifact, dict):
            raise ValueError("transcription artifact reference is required")
        model_input = TranscriptionInput(
            artifact=_artifact_ref(artifact),
            options=_options(payload, {"artifact", "request_id"}),
        )
        model_operation = ModelOperation.TRANSCRIPTION
    else:
        raise ValueError(f"unsupported unary compatibility operation {operation!r}")
    return ModelInvocation(
        model_call_id=call_id,
        route_id=route_id,
        task=f"compatibility.{operation}",
        operation=model_operation,
        input=model_input,
    )


def _options(payload: dict[str, JsonValue], excluded: set[str]) -> dict[str, JsonValue]:
    return {key: value for key, value in payload.items() if key not in excluded}


def _artifact_ref(value: dict[str, JsonValue]) -> ArtifactRef:
    required_strings = (
        "artifact_id",
        "representation",
        "kind",
        "mime_type",
        "content_ref",
        "digest",
    )
    if any(not isinstance(value.get(name), str) for name in required_strings):
        raise ValueError("artifact reference string fields are invalid")
    revision, size = value.get("revision"), value.get("size")
    if not isinstance(revision, int) or not isinstance(size, int):
        raise ValueError("artifact reference revision and size are invalid")
    retention, sensitivity = value.get("retention"), value.get("sensitivity")
    suggested_name = value.get("suggested_name", "")
    if not isinstance(retention, str) or not isinstance(sensitivity, str):
        raise ValueError("artifact reference policy fields are invalid")
    if not isinstance(suggested_name, str):
        raise ValueError("artifact suggested name is invalid")
    return ArtifactRef(
        artifact_id=cast(str, value["artifact_id"]),
        revision=revision,
        representation=cast(str, value["representation"]),
        kind=cast(str, value["kind"]),
        mime_type=cast(str, value["mime_type"]),
        content_ref=cast(str, value["content_ref"]),
        digest=cast(str, value["digest"]),
        size=size,
        retention=ArtifactRetention(retention),
        sensitivity=ArtifactSensitivity(sensitivity),
        suggested_name=suggested_name,
    )


def _response(output) -> dict[str, JsonValue]:
    if isinstance(output, EmbeddingOutput):
        return {
            "object": "list",
            "data": [
                {"object": "embedding", "index": index, "embedding": list(vector)}
                for index, vector in enumerate(output.vectors)
            ],
        }
    if isinstance(output, ImageGenerationOutput):
        return {
            "data": [
                *[dict(item) for item in output.provider_items],
                *[{"artifact": cast(JsonValue, asdict(artifact))} for artifact in output.artifacts],
            ]
        }
    if isinstance(output, SpeechOutput):
        return {"artifact": cast(JsonValue, asdict(output.artifact))}
    if isinstance(output, TranscriptionOutput):
        return {"text": output.text}
    raise RuntimeError("model gateway returned an incompatible finite output")


__all__ = ["ModelGatewayCompatibilityOwner"]
