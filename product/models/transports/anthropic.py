"""Retry-free Anthropic Messages transport preserving native event semantics."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from aiohttp.client import ClientTimeout
from aiohttp.client_reqrep import ClientResponse

from mote.contracts.inference.attempt import InferenceAttemptRequest
from mote.contracts.inference.governance import (
    CredentialHealthObservation,
    CredentialHealthVerdict,
    ProviderQuotaObservation,
    QuotaObservationKind,
)
from mote.contracts.inference.transport import ProviderWireResult
from mote.contracts.ports.inference.provider_transport import StreamSink, WireLifecycleSink
from mote.product.models.transports.connections.aiohttp import AioHttpConnectionLease
from mote.product.models.transports.openai import (
    ProviderProtocolError,
    _decode_json,
    _http_failure,
    _nonnegative_int_header,
    _positive_float_header,
    _read_bounded,
)
from mote.product.models.transports.translation import translate_anthropic_message, translate_anthropic_stream

AuthHeaders = Callable[[], Awaitable[Mapping[str, str]]]


class AnthropicMessagesTransport:
    def __init__(
        self,
        *,
        base_url: str,
        connection: AioHttpConnectionLease,
        auth_headers: AuthHeaders,
        anthropic_version: str = "2023-06-01",
        max_response_bytes: int = 16 * 1024 * 1024,
        max_stream_frame_bytes: int = 4 * 1024 * 1024,
    ) -> None:
        self._url = _messages_url(base_url)
        self._connection = connection
        self._auth_headers = auth_headers
        self._anthropic_version = anthropic_version
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
        headers = _anthropic_headers(await self._auth_headers(), anthropic_version=self._anthropic_version)
        async with self._connection.session.post(
            self._url,
            headers=headers,
            json=_anthropic_body(request, stream=stream is not None),
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
        _validate_anthropic_status(response, payload)
        _validate_message(payload)
        await lifecycle.response_started()
        canonical = translate_anthropic_message(payload)
        return _anthropic_result(
            request,
            response,
            canonical.model_dump(mode="json"),
            _message_usage(payload),
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
            _validate_anthropic_status(response, payload)
        state = "initial"
        chunks = 0
        input_tokens = 0
        output_tokens = 0
        observed_events: list[dict[str, Any]] = []
        while True:
            line = await response.content.readline()
            if not line:
                break
            if len(line) > self._max_stream_frame_bytes:
                raise ProviderProtocolError("Anthropic SSE frame exceeds configured limit")
            stripped = line.strip()
            if not stripped or stripped.startswith((b":", b"event:")):
                continue
            if not stripped.startswith(b"data:"):
                raise ProviderProtocolError("malformed Anthropic SSE field")
            payload = _decode_json(stripped[5:].strip())
            event_type = payload.get("type")
            if not isinstance(event_type, str):
                raise ProviderProtocolError("Anthropic stream event has no type")
            if event_type == "error":
                raise ProviderProtocolError("Anthropic stream reported error")
            if state == "initial":
                if event_type != "message_start":
                    raise ProviderProtocolError("Anthropic stream did not start with message_start")
                state = "active"
                message = payload.get("message")
                if isinstance(message, dict):
                    usage = message.get("usage")
                    if isinstance(usage, dict):
                        input_tokens = _nonnegative_int(usage.get("input_tokens")) or 0
                await lifecycle.response_started()
            elif event_type == "message_stop":
                state = "terminal"
            usage = payload.get("usage")
            if isinstance(usage, dict):
                observed = _nonnegative_int(usage.get("output_tokens"))
                if observed is not None:
                    output_tokens = observed
            chunks += 1
            observed_events.append(payload)
            await stream.emit(payload)
            if state == "terminal":
                break
        if state != "terminal":
            raise ProviderProtocolError("Anthropic stream ended without message_stop")
        canonical = translate_anthropic_stream(tuple(observed_events))
        return _anthropic_result(
            request,
            response,
            canonical.model_dump(mode="json"),
            input_tokens + output_tokens,
        )


def _messages_url(base_url: str) -> str:
    split = urlsplit(base_url)
    if split.scheme != "https" or not split.netloc or split.username or split.password:
        raise ValueError("provider base URL must be credential-free HTTPS")
    path = split.path.rstrip("/")
    if not path.endswith("/v1"):
        path += "/v1"
    return urlunsplit((split.scheme, split.netloc, path + "/messages", "", ""))


def _anthropic_headers(headers: Mapping[str, str], *, anthropic_version: str) -> dict[str, str]:
    forbidden = {"connection", "proxy-authorization", "transfer-encoding", "upgrade"}
    normalized = {str(key): str(value) for key, value in headers.items()}
    lowered = {key.lower() for key in normalized}
    if lowered & forbidden:
        raise ValueError("credential binding returned a forbidden hop-by-hop header")
    if "x-api-key" not in lowered and "authorization" not in lowered:
        raise ValueError("credential binding did not provide Anthropic authentication")
    return {
        **normalized,
        "anthropic-version": anthropic_version,
        "content-type": "application/json",
        "accept": "text/event-stream, application/json",
    }


def _anthropic_body(request: InferenceAttemptRequest, *, stream: bool) -> dict[str, Any]:
    invocation = request.invocation
    canonical = invocation.get("input")
    if isinstance(canonical, dict) and canonical.get("kind") == "generate":
        invocation = {
            "messages": _anthropic_canonical_messages(canonical),
            "system": canonical.get("system_prompt") or None,
            "tools": [
                {
                    "name": tool["name"],
                    "description": tool.get("description", ""),
                    "input_schema": tool.get("input_schema") or {},
                }
                for tool in canonical.get("tools") or ()
            ],
            "max_tokens": request.endpoint.execution_policy.max_output_tokens,
            "temperature": request.endpoint.execution_policy.temperature_micros / 1_000_000,
        }
    messages = invocation.get("messages")
    if not isinstance(messages, list):
        raise ValueError("Anthropic invocation requires messages list")
    max_tokens = invocation.get("max_tokens")
    if not isinstance(max_tokens, int) or max_tokens <= 0:
        raise ValueError("Anthropic invocation requires positive max_tokens")
    body: dict[str, Any] = {
        "model": request.endpoint.model,
        "messages": messages,
        "max_tokens": max_tokens,
        "stream": stream,
    }
    for key in (
        "system",
        "tools",
        "tool_choice",
        "thinking",
        "temperature",
        "top_p",
        "top_k",
        "metadata",
        "stop_sequences",
    ):
        if key in invocation:
            if invocation[key] is not None:
                body[key] = invocation[key]
    return body


def _anthropic_canonical_messages(
    canonical: dict[str, Any],
) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    for raw in canonical.get("messages") or ():
        if not isinstance(raw, dict):
            raise ValueError("canonical message must be an object")
        role = raw.get("role")
        content = raw.get("content")
        if role == "tool":
            tool_call_id = raw.get("tool_call_id")
            if not isinstance(tool_call_id, str) or not tool_call_id:
                raise ValueError("canonical tool result requires tool_call_id")
            messages.append(
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": tool_call_id,
                            "content": content,
                        }
                    ],
                }
            )
            continue
        if role not in {"user", "assistant"}:
            raise ValueError("Anthropic canonical message role is unsupported")
        if isinstance(content, list):
            blocks: list[dict[str, Any]] = list(content)
        elif content in (None, ""):
            blocks = []
        else:
            blocks = [{"type": "text", "text": str(content)}]
        for call in raw.get("tool_calls") or ():
            blocks.append(
                {
                    "type": "tool_use",
                    "id": call.get("id", ""),
                    "name": call["name"],
                    "input": call.get("arguments") or {},
                }
            )
        messages.append({"role": role, "content": blocks or ""})
    return messages


def _validate_anthropic_status(response: ClientResponse, payload: dict[str, Any]) -> None:
    if payload.get("type") == "error" or response.status < 200 or response.status >= 300:
        raise ProviderProtocolError(
            "Anthropic returned an error envelope",
            disposition=_http_failure(response.status),
            retry_after_seconds=_positive_float_header(response.headers.get("retry-after")),
        )


def _validate_message(payload: dict[str, Any]) -> None:
    if payload.get("type") != "message" or not isinstance(payload.get("id"), str):
        raise ProviderProtocolError("Anthropic response is not a message")
    if not isinstance(payload.get("content"), list):
        raise ProviderProtocolError("Anthropic message has no content array")
    if not isinstance(payload.get("usage"), dict):
        raise ProviderProtocolError("Anthropic message has no usage")


def _message_usage(payload: dict[str, Any]) -> int | None:
    usage = payload.get("usage")
    if not isinstance(usage, dict):
        return None
    input_tokens = _nonnegative_int(usage.get("input_tokens"))
    output_tokens = _nonnegative_int(usage.get("output_tokens"))
    if input_tokens is None or output_tokens is None:
        return None
    return input_tokens + output_tokens


def _anthropic_result(
    request: InferenceAttemptRequest,
    response: ClientResponse,
    payload: dict[str, Any],
    usage_units: int | None,
) -> ProviderWireResult:
    return ProviderWireResult(
        payload=payload,
        usage_units=usage_units,
        quota_observation=_anthropic_quota(request, response),
        credential_observation=CredentialHealthObservation(
            credential_slot_id=request.credential_slot_id,
            credential_version=request.credential_version,
            verdict=CredentialHealthVerdict.SUCCESS,
            reason="provider request succeeded",
        ),
    )


def _anthropic_quota(request: InferenceAttemptRequest, response: ClientResponse) -> ProviderQuotaObservation | None:
    requests = _nonnegative_int_header(response.headers.get("anthropic-ratelimit-requests-remaining"))
    tokens = _nonnegative_int_header(response.headers.get("anthropic-ratelimit-tokens-remaining"))
    retry_after = _positive_float_header(response.headers.get("retry-after"))
    if requests is None and tokens is None and retry_after is None:
        return None
    return ProviderQuotaObservation(
        provider=request.endpoint.provider,
        endpoint_id=request.endpoint.endpoint_id,
        credential_slot_id=request.credential_slot_id,
        kind=(QuotaObservationKind.RETRY_AFTER if retry_after is not None else QuotaObservationKind.LIMITS),
        remaining_requests=requests,
        remaining_tokens=tokens,
        retry_after_seconds=retry_after,
    )


def _nonnegative_int(value: object) -> int | None:
    return value if isinstance(value, int) and value >= 0 else None
