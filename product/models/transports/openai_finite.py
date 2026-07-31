"""Retry-free OpenAI finite model-operation transport."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from aiohttp import FormData
from aiohttp.client import ClientTimeout

from mote.contracts.artifact import ArtifactRef
from mote.contracts.inference.attempt import InferenceAttemptRequest
from mote.contracts.inference.transport import ProviderWireResult
from mote.contracts.model.invocation import (
    CanonicalModelResponse,
    EmbeddingOutput,
    ImageGenerationOutput,
    SpeechOutput,
    TranscriptionOutput,
)
from mote.contracts.ports.inference.provider_transport import GenerateTransport, StreamSink, WireLifecycleSink
from mote.product.models.transports.artifact_io import ArtifactPublisher, ArtifactResolver
from mote.product.models.transports.connections.aiohttp import AioHttpConnectionLease
from mote.product.models.transports.openai import (
    AuthHeaders,
    _decode_json,
    _read_bounded,
    _transport_result,
    _usage_units,
    _validate_status_and_payload,
    _validated_headers,
)
from mote.product.models.transports.translation import _openai_usage


class OpenAIFiniteTransport:
    def __init__(
        self,
        *,
        base_url: str,
        connection: AioHttpConnectionLease,
        auth_headers: AuthHeaders,
        max_response_bytes: int = 16 * 1024 * 1024,
        artifact_resolver: ArtifactResolver | None = None,
        artifact_publisher: ArtifactPublisher | None = None,
        allow_http_for_testing: bool = False,
    ) -> None:
        if max_response_bytes <= 0:
            raise ValueError("finite response limit must be positive")
        self._base_url = _v1_url(base_url, allow_http_for_testing=allow_http_for_testing)
        self._connection = connection
        self._auth_headers = auth_headers
        self._max_response_bytes = max_response_bytes
        self._artifact_resolver = artifact_resolver
        self._artifact_publisher = artifact_publisher
        self._closed = False

    async def generate_once(
        self,
        request: InferenceAttemptRequest,
        *,
        local_deadline: float,
        lifecycle: WireLifecycleSink,
        stream: StreamSink | None,
    ) -> ProviderWireResult:
        if self._closed:
            raise RuntimeError("finite transport is closed")
        if stream is not None:
            raise ValueError("finite operation transport does not support streaming")
        remaining = local_deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            raise TimeoutError("provider deadline exceeded before finite operation")
        operation, body = _request_body(request)
        headers = {
            **_validated_headers(await self._auth_headers()),
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        if request.invocation.get("operation") == "transcription":
            return await self._transcription(request, operation, remaining, lifecycle)
        async with self._connection.session.post(
            self._base_url + operation,
            headers=headers,
            json=body,
            timeout=ClientTimeout(total=remaining),
            allow_redirects=False,
            trace_request_ctx={"lifecycle": lifecycle},
        ) as response:
            if request.invocation.get("operation") == "speech":
                _validate_status_and_payload(
                    response.status,
                    {}
                    if 200 <= response.status < 300
                    else _decode_json(await _read_bounded(response, self._max_response_bytes)),
                )
                content_type = response.headers.get("Content-Type", "").split(";", 1)[0]
                if not content_type.startswith("audio/"):
                    raise ValueError("speech response is not audio")
                content = await _read_binary(response, self._max_response_bytes)
                if not content:
                    raise ValueError("speech response is empty")
                if self._artifact_publisher is None:
                    raise RuntimeError("speech artifact publisher is unavailable")
                artifact = await self._artifact_publisher(content, content_type, _speech_name(content_type))
                await lifecycle.response_started()
                canonical = CanonicalModelResponse(output=SpeechOutput(artifact=artifact))
                return _transport_result(request, response, canonical.model_dump(mode="json"), None)
            payload = _decode_json(await _read_bounded(response, self._max_response_bytes))
            _validate_status_and_payload(response.status, payload)
            canonical = _translate(request, payload)
            await lifecycle.response_started()
            return _transport_result(
                request,
                response,
                canonical.model_dump(mode="json"),
                _usage_units(payload),
            )

    async def _transcription(
        self,
        request: InferenceAttemptRequest,
        operation: str,
        remaining: float,
        lifecycle: WireLifecycleSink,
    ) -> ProviderWireResult:
        if self._artifact_resolver is None:
            raise RuntimeError("transcription artifact resolver is unavailable")
        value = request.invocation.get("input")
        if not isinstance(value, dict) or not isinstance(value.get("artifact"), dict):
            raise ValueError("transcription artifact reference is required")
        try:
            ref = ArtifactRef(**value["artifact"])
        except (TypeError, ValueError) as exc:
            raise ValueError("transcription artifact reference is invalid") from exc
        if ref.size > self._max_response_bytes:
            raise ValueError("transcription input exceeds configured limit")
        resolved = await self._artifact_resolver(ref)
        if resolved.ref != ref:
            raise RuntimeError("artifact resolver changed transcription identity")
        form = FormData(quote_fields=True)
        form.add_field(
            "file",
            resolved.content,
            filename=ref.suggested_name or "audio.bin",
            content_type=ref.mime_type,
        )
        form.add_field("model", request.endpoint.model)
        options = value.get("options") or {}
        if not isinstance(options, dict):
            raise ValueError("transcription options must be an object")
        for key, option in options.items():
            if not isinstance(key, str) or not isinstance(option, (str, int, float)):
                raise ValueError("transcription option must be scalar")
            form.add_field(key, str(option))
        headers = {
            **_validated_headers(await self._auth_headers()),
            "Accept": "application/json",
        }
        async with self._connection.session.post(
            self._base_url + operation,
            headers=headers,
            data=form,
            timeout=ClientTimeout(total=remaining),
            allow_redirects=False,
            trace_request_ctx={"lifecycle": lifecycle},
        ) as response:
            payload = _decode_json(await _read_bounded(response, self._max_response_bytes))
            _validate_status_and_payload(response.status, payload)
            canonical = _translate(request, payload)
            await lifecycle.response_started()
            return _transport_result(
                request,
                response,
                canonical.model_dump(mode="json"),
                _usage_units(payload),
            )

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        await self._connection.release()


class ProductFiniteTransportResolver:
    def __init__(
        self,
        generate_transports: Mapping[tuple[str, str], GenerateTransport],
        finite_transports: Mapping[tuple[str, str], GenerateTransport],
    ) -> None:
        self._generate = dict(generate_transports)
        self._finite = dict(finite_transports)

    def resolve_generate(self, request: InferenceAttemptRequest) -> GenerateTransport:
        operation = request.invocation.get("operation", "generate")
        key = (request.endpoint.transport, request.credential_slot_id)
        source = self._generate if operation == "generate" else self._finite
        try:
            return source[key]
        except KeyError as exc:
            raise LookupError(f"no transport for operation {operation!r}, protocol and credential slot") from exc


def _request_body(
    request: InferenceAttemptRequest,
) -> tuple[str, dict[str, Any]]:
    invocation = request.invocation
    operation = invocation.get("operation")
    value = invocation.get("input")
    if not isinstance(value, dict):
        raise ValueError("finite invocation input must be an object")
    if operation == "embedding" and value.get("kind") == "embedding":
        values = value.get("values")
        if not isinstance(values, list) or not values:
            raise ValueError("embedding values are required")
        body: dict[str, Any] = {"model": request.endpoint.model, "input": values}
        if value.get("dimensions") is not None:
            body["dimensions"] = value["dimensions"]
        return "/embeddings", body
    if operation == "image_generation" and value.get("kind") == "image_generation":
        prompt = value.get("prompt")
        if not isinstance(prompt, str) or not prompt:
            raise ValueError("image generation prompt is required")
        options = value.get("options") or {}
        if not isinstance(options, dict):
            raise ValueError("image generation options must be an object")
        return "/images/generations", {"model": request.endpoint.model, "prompt": prompt, **options}
    if operation == "speech" and value.get("kind") == "speech":
        text, voice = value.get("text"), value.get("voice")
        if not isinstance(text, str) or not text or not isinstance(voice, str) or not voice:
            raise ValueError("speech text and voice are required")
        options = value.get("options") or {}
        if not isinstance(options, dict):
            raise ValueError("speech options must be an object")
        return "/audio/speech", {
            "model": request.endpoint.model,
            "input": text,
            "voice": voice,
            **options,
        }
    if operation == "transcription" and value.get("kind") == "transcription":
        return "/audio/transcriptions", {}
    raise ValueError(f"unsupported finite operation {operation!r}")


def _translate(request: InferenceAttemptRequest, payload: dict[str, Any]) -> CanonicalModelResponse:
    operation = request.invocation.get("operation")
    if operation == "embedding":
        data = payload.get("data")
        if not isinstance(data, list):
            raise ValueError("embedding response has no data array")
        ordered: list[tuple[int, tuple[float, ...]]] = []
        for position, item in enumerate(data):
            if not isinstance(item, dict) or not isinstance(item.get("embedding"), list):
                raise ValueError("embedding response item is invalid")
            vector = item["embedding"]
            if not all(isinstance(value, (int, float)) for value in vector):
                raise ValueError("embedding vector contains a non-number")
            index = item.get("index", position)
            if not isinstance(index, int) or index < 0:
                raise ValueError("embedding index is invalid")
            ordered.append((index, tuple(float(value) for value in vector)))
        ordered.sort(key=lambda item: item[0])
        return CanonicalModelResponse(
            output=EmbeddingOutput(vectors=tuple(vector for _, vector in ordered)),
            usage=_openai_usage(payload.get("usage")),
            provider_request_id=str(payload["id"]) if payload.get("id") else None,
        )
    if operation == "image_generation":
        data = payload.get("data")
        if not isinstance(data, list) or not all(isinstance(item, dict) for item in data):
            raise ValueError("image response has invalid data array")
        return CanonicalModelResponse(
            output=ImageGenerationOutput(provider_items=tuple(data)),
            usage=_openai_usage(payload.get("usage")),
            provider_request_id=str(payload["id"]) if payload.get("id") else None,
        )
    if operation == "transcription":
        text = payload.get("text")
        if not isinstance(text, str):
            raise ValueError("transcription response has no text")
        return CanonicalModelResponse(
            output=TranscriptionOutput(text=text),
            usage=_openai_usage(payload.get("usage")),
            provider_request_id=str(payload["id"]) if payload.get("id") else None,
        )
    raise ValueError(f"unsupported finite response {operation!r}")


def _v1_url(base_url: str, *, allow_http_for_testing: bool = False) -> str:
    split = urlsplit(base_url)
    schemes = {"https", "http"} if allow_http_for_testing else {"https"}
    if split.scheme not in schemes or not split.netloc or split.username or split.password:
        raise ValueError("provider base URL must be credential-free HTTPS")
    path = split.path.rstrip("/")
    if not path.endswith("/v1"):
        path += "/v1"
    return urlunsplit((split.scheme, split.netloc, path, "", ""))


async def _read_binary(response, limit: int) -> bytes:
    if response.content_length is not None and response.content_length > limit:
        raise ValueError("provider binary response exceeds configured limit")
    content = await response.content.read(limit + 1)
    if len(content) > limit:
        raise ValueError("provider binary response exceeds configured limit")
    return content


def _speech_name(content_type: str) -> str:
    extension = {
        "audio/mpeg": "mp3",
        "audio/wav": "wav",
        "audio/ogg": "ogg",
        "audio/flac": "flac",
    }.get(content_type, "bin")
    return f"speech.{extension}"


__all__ = ["OpenAIFiniteTransport", "ProductFiniteTransportResolver"]
