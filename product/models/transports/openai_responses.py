"""Retry-free OpenAI Responses transport with strict lifecycle validation."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from aiohttp.client import ClientTimeout
from aiohttp.client_reqrep import ClientResponse

from mote.contracts.inference.attempt import InferenceAttemptRequest
from mote.contracts.inference.transport import ProviderWireResult
from mote.contracts.ports.inference.provider_transport import StreamSink, WireLifecycleSink
from mote.product.models.transports.connections.aiohttp import AioHttpConnectionLease
from mote.product.models.transports.openai import (
    ProviderProtocolError,
    _decode_json,
    _positive_float_header,
    _read_bounded,
    _transport_result,
    _usage_units,
    _validate_status_and_payload,
    _validated_headers,
)
from mote.product.models.transports.translation import translate_openai_responses

AuthHeaders = Callable[[], Awaitable[Mapping[str, str]]]


class OpenAIResponsesTransport:
    def __init__(
        self,
        *,
        base_url: str,
        connection: AioHttpConnectionLease,
        auth_headers: AuthHeaders,
        max_response_bytes: int = 16 * 1024 * 1024,
        max_stream_frame_bytes: int = 4 * 1024 * 1024,
    ) -> None:
        self._url = _responses_url(base_url)
        self._connection = connection
        self._auth_headers = auth_headers
        self._max_response_bytes = max_response_bytes
        self._max_stream_frame_bytes = max_stream_frame_bytes
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
            raise RuntimeError("transport is closed")
        remaining = local_deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            raise TimeoutError("provider deadline exceeded before request")
        headers = _validated_headers(await self._auth_headers())
        headers = {
            **headers,
            "Content-Type": "application/json",
            "Accept": "text/event-stream" if stream else "application/json",
        }
        async with self._connection.session.post(
            self._url,
            headers=headers,
            json=_responses_body(request, stream=stream is not None),
            timeout=ClientTimeout(total=remaining),
            allow_redirects=False,
            trace_request_ctx={"lifecycle": lifecycle},
        ) as response:
            if stream is None:
                return await self._unary(response, request, lifecycle)
            return await self._stream(response, request, lifecycle, stream)

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        await self._connection.release()

    async def _unary(
        self,
        response: ClientResponse,
        request: InferenceAttemptRequest,
        lifecycle: WireLifecycleSink,
    ) -> ProviderWireResult:
        payload = _decode_json(await _read_bounded(response, self._max_response_bytes))
        _validate_status_and_payload(
            response.status,
            payload,
            retry_after_seconds=_positive_float_header(response.headers.get("retry-after")),
        )
        _validate_completed_response(payload)
        await lifecycle.response_started()
        canonical = translate_openai_responses(payload)
        return _transport_result(
            request,
            response,
            canonical.model_dump(mode="json"),
            _responses_usage(payload),
        )

    async def _stream(
        self,
        response: ClientResponse,
        request: InferenceAttemptRequest,
        lifecycle: WireLifecycleSink,
        stream: StreamSink,
    ) -> ProviderWireResult:
        if response.status < 200 or response.status >= 300:
            payload = _decode_json(await _read_bounded(response, self._max_response_bytes))
            _validate_status_and_payload(
                response.status,
                payload,
                retry_after_seconds=_positive_float_header(response.headers.get("retry-after")),
            )
        started = False
        terminal_payload: dict[str, Any] | None = None
        while True:
            line = await response.content.readline()
            if not line:
                break
            if len(line) > self._max_stream_frame_bytes:
                raise ProviderProtocolError("Responses SSE frame exceeds configured limit")
            stripped = line.strip()
            if not stripped or stripped.startswith((b":", b"event:")):
                continue
            if not stripped.startswith(b"data:"):
                raise ProviderProtocolError("malformed Responses SSE field")
            payload = _decode_json(stripped[5:].strip())
            event_type = payload.get("type")
            if not isinstance(event_type, str) or not event_type:
                raise ProviderProtocolError("Responses stream event has no type")
            if event_type in {"response.failed", "response.incomplete", "error"}:
                raise ProviderProtocolError("Responses stream reported terminal failure")
            if not started:
                await lifecycle.response_started()
                started = True
            await stream.emit(payload)
            if event_type == "response.completed":
                response_payload = payload.get("response")
                if not isinstance(response_payload, dict):
                    raise ProviderProtocolError("completed Responses event has no response")
                _validate_completed_response(response_payload)
                terminal_payload = response_payload
                break
        if terminal_payload is None:
            raise ProviderProtocolError("Responses stream ended without response.completed")
        canonical = translate_openai_responses(terminal_payload)
        return _transport_result(
            request,
            response,
            canonical.model_dump(mode="json"),
            _responses_usage(terminal_payload),
        )


def _responses_url(base_url: str) -> str:
    split = urlsplit(base_url)
    if split.scheme != "https" or not split.netloc or split.username or split.password:
        raise ValueError("provider base URL must be credential-free HTTPS")
    path = split.path.rstrip("/")
    if not path.endswith("/v1"):
        path += "/v1"
    return urlunsplit((split.scheme, split.netloc, path + "/responses", "", ""))


def _responses_body(request: InferenceAttemptRequest, *, stream: bool) -> dict[str, Any]:
    invocation = request.invocation
    canonical = invocation.get("input")
    if isinstance(canonical, dict) and canonical.get("kind") == "generate":
        invocation = {
            "input": list(canonical.get("messages") or ()),
            "instructions": canonical.get("system_prompt") or None,
            "tools": [
                {
                    "type": "function",
                    "name": tool["name"],
                    "description": tool.get("description", ""),
                    "parameters": tool.get("input_schema") or {},
                }
                for tool in canonical.get("tools") or ()
            ],
            "text": (
                {
                    "format": {
                        "type": "json_schema",
                        **canonical["output_schema"],
                    }
                }
                if canonical.get("output_schema") is not None
                else None
            ),
            "max_output_tokens": request.endpoint.execution_policy.max_output_tokens,
            "temperature": request.endpoint.execution_policy.temperature_micros / 1_000_000,
        }
    if "input" not in invocation:
        raise ValueError("OpenAI Responses invocation requires input")
    body: dict[str, Any] = {
        "model": request.endpoint.model,
        "input": invocation["input"],
        "stream": stream,
    }
    for key in (
        "instructions",
        "tools",
        "tool_choice",
        "reasoning",
        "max_output_tokens",
        "temperature",
        "top_p",
        "metadata",
        "text",
        "include",
    ):
        if key in invocation:
            if invocation[key] is not None:
                body[key] = invocation[key]
    return body


def _validate_completed_response(payload: dict[str, Any]) -> None:
    if not isinstance(payload.get("id"), str):
        raise ProviderProtocolError("Responses payload has no id")
    if payload.get("status") != "completed":
        raise ProviderProtocolError("Responses payload is not completed")
    if not isinstance(payload.get("output"), list):
        raise ProviderProtocolError("Responses payload has no output array")


def _responses_usage(payload: dict[str, Any]) -> int | None:
    usage = payload.get("usage")
    if not isinstance(usage, dict):
        return None
    normalized = {
        "total_tokens": usage.get("total_tokens"),
        "prompt_tokens": usage.get("input_tokens"),
        "completion_tokens": usage.get("output_tokens"),
    }
    return _usage_units({"usage": normalized})
