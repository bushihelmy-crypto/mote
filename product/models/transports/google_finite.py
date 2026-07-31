"""Retry-free Gemini finite-operation transport."""

from __future__ import annotations

import asyncio
import base64
from collections.abc import Awaitable, Callable, Mapping
from typing import Any
from urllib.parse import quote, urlsplit, urlunsplit

from aiohttp.client import ClientTimeout

from mote.contracts.inference.attempt import InferenceAttemptRequest
from mote.contracts.inference.transport import ProviderWireResult
from mote.contracts.model.invocation import CanonicalModelResponse, EmbeddingOutput, ImageGenerationOutput
from mote.contracts.ports.inference.provider_transport import StreamSink, WireLifecycleSink
from mote.product.models.transports.artifact_io import ArtifactPublisher
from mote.product.models.transports.connections.aiohttp import AioHttpConnectionLease
from mote.product.models.transports.openai import (
    _decode_json,
    _read_bounded,
    _transport_result,
    _validate_status_and_payload,
)

AuthHeaders = Callable[[], Awaitable[Mapping[str, str]]]


class GoogleFiniteTransport:
    def __init__(
        self,
        *,
        base_url: str,
        connection: AioHttpConnectionLease,
        auth_headers: AuthHeaders,
        artifact_publisher: ArtifactPublisher | None = None,
        max_response_bytes: int = 16 * 1024 * 1024,
        allow_http_for_testing: bool = False,
    ) -> None:
        _operation_url(
            base_url,
            "model",
            "embedContent",
            allow_http_for_testing=allow_http_for_testing,
        )
        self._base_url = base_url
        self._connection = connection
        self._auth_headers = auth_headers
        self._artifact_publisher = artifact_publisher
        self._max_response_bytes = max_response_bytes
        self._allow_http_for_testing = allow_http_for_testing
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
        method, body = _request(request)
        headers = {
            **{str(key): str(value) for key, value in (await self._auth_headers()).items()},
            "content-type": "application/json",
            "accept": "application/json",
        }
        async with self._connection.session.post(
            _operation_url(
                self._base_url,
                request.endpoint.model,
                method,
                allow_http_for_testing=self._allow_http_for_testing,
            ),
            headers=headers,
            json=body,
            timeout=ClientTimeout(total=remaining),
            allow_redirects=False,
            trace_request_ctx={"lifecycle": lifecycle},
        ) as response:
            payload = _decode_json(await _read_bounded(response, self._max_response_bytes))
            _validate_status_and_payload(response.status, payload)
            canonical = await self._translate(request, payload)
            await lifecycle.response_started()
            return _transport_result(request, response, canonical.model_dump(mode="json"), None)

    async def _translate(self, request: InferenceAttemptRequest, payload: dict[str, Any]) -> CanonicalModelResponse:
        operation = request.invocation.get("operation")
        if operation == "embedding":
            raw = payload.get("embeddings")
            if raw is None and isinstance(payload.get("embedding"), dict):
                raw = [payload["embedding"]]
            if not isinstance(raw, list) or not raw:
                raise ValueError("Gemini embedding response has no embeddings")
            vectors = []
            for item in raw:
                values = item.get("values") if isinstance(item, dict) else None
                if not isinstance(values, list) or not all(isinstance(value, (int, float)) for value in values):
                    raise ValueError("Gemini embedding vector is invalid")
                vectors.append(tuple(float(value) for value in values))
            return CanonicalModelResponse(output=EmbeddingOutput(vectors=tuple(vectors)))
        if operation == "image_generation":
            predictions = payload.get("predictions")
            if not isinstance(predictions, list) or not predictions:
                raise ValueError("Gemini image response has no predictions")
            if self._artifact_publisher is None:
                raise RuntimeError("image artifact publisher is unavailable")
            artifacts = []
            for index, item in enumerate(predictions):
                encoded = item.get("bytesBase64Encoded") if isinstance(item, dict) else None
                if not isinstance(encoded, str):
                    raise ValueError("Gemini image prediction has no image bytes")
                try:
                    content = base64.b64decode(encoded, validate=True)
                except ValueError as exc:
                    raise ValueError("Gemini image prediction is not valid base64") from exc
                mime_type = str(item.get("mimeType") or "image/png")
                artifacts.append(await self._artifact_publisher(content, mime_type, f"generated-{index + 1}.png"))
            return CanonicalModelResponse(output=ImageGenerationOutput(artifacts=tuple(artifacts)))
        raise ValueError(f"unsupported Gemini finite response {operation!r}")

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        await self._connection.release()


def _request(request: InferenceAttemptRequest) -> tuple[str, dict[str, Any]]:
    value = request.invocation.get("input")
    if not isinstance(value, dict):
        raise ValueError("Gemini finite invocation input must be an object")
    operation = request.invocation.get("operation")
    if operation == "embedding" and value.get("kind") == "embedding":
        values = value.get("values")
        if not isinstance(values, list) or not values:
            raise ValueError("Gemini embedding values are required")
        requests = [
            {"model": f"models/{request.endpoint.model}", "content": {"parts": [{"text": text}]}} for text in values
        ]
        dimensions = value.get("dimensions")
        if dimensions is not None:
            for item in requests:
                item["outputDimensionality"] = dimensions
        return "batchEmbedContents", {"requests": requests}
    if operation == "image_generation" and value.get("kind") == "image_generation":
        prompt = value.get("prompt")
        if not isinstance(prompt, str) or not prompt:
            raise ValueError("Gemini image prompt is required")
        options = value.get("options") or {}
        if not isinstance(options, dict):
            raise ValueError("Gemini image options must be an object")
        return "predict", {"instances": [{"prompt": prompt}], "parameters": options}
    raise ValueError(f"unsupported Gemini finite operation {operation!r}")


def _operation_url(base_url: str, model: str, method: str, *, allow_http_for_testing: bool = False) -> str:
    split = urlsplit(base_url)
    schemes = {"https", "http"} if allow_http_for_testing else {"https"}
    if split.scheme not in schemes or not split.netloc or split.username or split.password:
        raise ValueError("provider base URL must be credential-free HTTPS")
    path = split.path.rstrip("/")
    if not path.endswith("/v1beta"):
        path += "/v1beta"
    path += f"/models/{quote(model, safe='')}:" + method
    return urlunsplit((split.scheme, split.netloc, path, "", ""))


__all__ = ["GoogleFiniteTransport"]
