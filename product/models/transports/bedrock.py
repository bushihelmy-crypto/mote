"""Bedrock Anthropic transport with native SigV4 and AWS EventStream parsing."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import struct
import zlib
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote, urlsplit, urlunsplit

from aiohttp.client import ClientTimeout
from aiohttp.client_reqrep import ClientResponse

from mote.contracts.inference.attempt import InferenceAttemptRequest
from mote.contracts.inference.governance import CredentialHealthObservation, CredentialHealthVerdict
from mote.contracts.inference.transport import ProviderWireResult
from mote.contracts.ports.inference.provider_transport import StreamSink, WireLifecycleSink
from mote.product.models.transports.anthropic import _anthropic_canonical_messages, _message_usage, _validate_message
from mote.product.models.transports.connections.aiohttp import AioHttpConnectionLease
from mote.product.models.transports.openai import ProviderProtocolError, _decode_json, _http_failure, _read_bounded
from mote.product.models.transports.translation import translate_anthropic_message, translate_anthropic_stream


@dataclass(frozen=True, slots=True)
class AwsCredentials:
    access_key_id: str
    secret_access_key: str
    session_token: str | None = None

    def __post_init__(self) -> None:
        if not self.access_key_id or not self.secret_access_key:
            raise ValueError("AWS credentials are incomplete")


CredentialProvider = Callable[[], Awaitable[AwsCredentials]]


@dataclass(frozen=True, slots=True)
class EventStreamMessage:
    headers: Mapping[str, object]
    payload: bytes


class AwsEventStreamDecoder:
    def __init__(self, *, max_frame_bytes: int) -> None:
        if max_frame_bytes < 16:
            raise ValueError("EventStream frame limit is too small")
        self._max_frame_bytes = max_frame_bytes
        self._buffer = bytearray()

    def feed(self, data: bytes) -> tuple[EventStreamMessage, ...]:
        self._buffer.extend(data)
        messages: list[EventStreamMessage] = []
        while len(self._buffer) >= 12:
            total_length, headers_length, prelude_crc = struct.unpack(">III", self._buffer[:12])
            if total_length < 16 or total_length > self._max_frame_bytes:
                raise ProviderProtocolError("AWS EventStream frame length is invalid")
            if headers_length > total_length - 16:
                raise ProviderProtocolError("AWS EventStream headers length is invalid")
            if zlib.crc32(self._buffer[:8]) & 0xFFFFFFFF != prelude_crc:
                raise ProviderProtocolError("AWS EventStream prelude CRC mismatch")
            if len(self._buffer) < total_length:
                break
            frame = bytes(self._buffer[:total_length])
            del self._buffer[:total_length]
            expected_crc = struct.unpack(">I", frame[-4:])[0]
            if zlib.crc32(frame[:-4]) & 0xFFFFFFFF != expected_crc:
                raise ProviderProtocolError("AWS EventStream message CRC mismatch")
            headers_end = 12 + headers_length
            messages.append(
                EventStreamMessage(
                    headers=_decode_eventstream_headers(frame[12:headers_end]),
                    payload=frame[headers_end:-4],
                )
            )
        return tuple(messages)

    def finish(self) -> None:
        if self._buffer:
            raise ProviderProtocolError("AWS EventStream ended with a partial frame")


class BedrockAnthropicTransport:
    def __init__(
        self,
        *,
        base_url: str,
        region: str,
        connection: AioHttpConnectionLease,
        credentials: CredentialProvider,
        max_response_bytes: int = 16 * 1024 * 1024,
        max_stream_frame_bytes: int = 4 * 1024 * 1024,
    ) -> None:
        _bedrock_url(base_url, "model", stream=False)
        if not region:
            raise ValueError("AWS region is required")
        self._base_url = base_url
        self._region = region
        self._connection = connection
        self._credentials = credentials
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
        url = _bedrock_url(
            self._base_url,
            request.endpoint.model,
            stream=stream is not None,
        )
        body = json.dumps(
            _bedrock_anthropic_body(request),
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        headers = _sign_sigv4(
            method="POST",
            url=url,
            headers={
                "content-type": "application/json",
                "accept": ("application/vnd.amazon.eventstream" if stream is not None else "application/json"),
            },
            body=body,
            credentials=await self._credentials(),
            region=self._region,
            service="bedrock",
            now=datetime.now(timezone.utc),
        )
        async with self._connection.session.post(
            url,
            headers=headers,
            data=body,
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
        _validate_bedrock_status(response, payload)
        _validate_message(payload)
        await lifecycle.response_started()
        canonical = translate_anthropic_message(payload)
        return _bedrock_result(request, canonical.model_dump(mode="json"), _message_usage(payload))

    async def _stream(
        self,
        response: ClientResponse,
        request: InferenceAttemptRequest,
        lifecycle: WireLifecycleSink,
        stream: StreamSink,
    ) -> ProviderWireResult:
        if response.status < 200 or response.status >= 300:
            payload = _decode_json(await _read_bounded(response, self._max_response_bytes))
            _validate_bedrock_status(response, payload)
        decoder = AwsEventStreamDecoder(max_frame_bytes=self._max_stream_frame_bytes)
        started = False
        terminal = False
        chunks = 0
        input_tokens = 0
        output_tokens = 0
        observed_events: list[dict[str, Any]] = []
        async for data in response.content.iter_chunked(64 * 1024):
            for message in decoder.feed(data):
                payload = _decode_bedrock_event(message)
                event_type = payload.get("type")
                if not isinstance(event_type, str):
                    raise ProviderProtocolError("Bedrock Anthropic event has no type")
                if not started:
                    if event_type != "message_start":
                        raise ProviderProtocolError("Bedrock Anthropic stream did not start with message_start")
                    await lifecycle.response_started()
                    started = True
                message_payload = payload.get("message")
                if isinstance(message_payload, dict):
                    usage = message_payload.get("usage")
                    if isinstance(usage, dict):
                        input_tokens = _nonnegative_int(usage.get("input_tokens")) or 0
                usage = payload.get("usage")
                if isinstance(usage, dict):
                    observed = _nonnegative_int(usage.get("output_tokens"))
                    if observed is not None:
                        output_tokens = observed
                chunks += 1
                observed_events.append(payload)
                await stream.emit(payload)
                if event_type == "message_stop":
                    terminal = True
                    break
            if terminal:
                break
        decoder.finish()
        if not terminal:
            raise ProviderProtocolError("Bedrock Anthropic stream ended without message_stop")
        canonical = translate_anthropic_stream(tuple(observed_events))
        return _bedrock_result(
            request,
            canonical.model_dump(mode="json"),
            input_tokens + output_tokens,
        )


def _bedrock_url(base_url: str, model: str, *, stream: bool) -> str:
    split = urlsplit(base_url)
    if split.scheme != "https" or not split.netloc or split.username or split.password:
        raise ValueError("Bedrock base URL must be credential-free HTTPS")
    operation = "invoke-with-response-stream" if stream else "invoke"
    path = split.path.rstrip("/") + f"/model/{quote(model, safe='')}/{operation}"
    return urlunsplit((split.scheme, split.netloc, path, "", ""))


def _bedrock_anthropic_body(request: InferenceAttemptRequest) -> dict[str, Any]:
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
    max_tokens = invocation.get("max_tokens")
    if not isinstance(messages, list):
        raise ValueError("Bedrock Anthropic invocation requires messages list")
    if not isinstance(max_tokens, int) or max_tokens <= 0:
        raise ValueError("Bedrock Anthropic invocation requires positive max_tokens")
    body: dict[str, Any] = {
        "anthropic_version": invocation.get("anthropic_version", "bedrock-2023-05-31"),
        "messages": messages,
        "max_tokens": max_tokens,
    }
    for key in (
        "system",
        "tools",
        "tool_choice",
        "thinking",
        "temperature",
        "top_p",
        "top_k",
        "stop_sequences",
    ):
        if key in invocation:
            if invocation[key] is not None:
                body[key] = invocation[key]
    return body


def _sign_sigv4(
    *,
    method: str,
    url: str,
    headers: Mapping[str, str],
    body: bytes,
    credentials: AwsCredentials,
    region: str,
    service: str,
    now: datetime,
) -> dict[str, str]:
    split = urlsplit(url)
    timestamp = now.astimezone(timezone.utc)
    amz_date = timestamp.strftime("%Y%m%dT%H%M%SZ")
    date_stamp = timestamp.strftime("%Y%m%d")
    payload_hash = hashlib.sha256(body).hexdigest()
    signed = {key.lower().strip(): " ".join(value.strip().split()) for key, value in headers.items()}
    signed["host"] = split.netloc
    signed["x-amz-content-sha256"] = payload_hash
    signed["x-amz-date"] = amz_date
    if credentials.session_token:
        signed["x-amz-security-token"] = credentials.session_token
    signed_names = ";".join(sorted(signed))
    canonical_headers = "".join(f"{key}:{signed[key]}\n" for key in sorted(signed))
    canonical_request = "\n".join(
        (
            method.upper(),
            split.path or "/",
            split.query,
            canonical_headers,
            signed_names,
            payload_hash,
        )
    )
    scope = f"{date_stamp}/{region}/{service}/aws4_request"
    string_to_sign = "\n".join(
        (
            "AWS4-HMAC-SHA256",
            amz_date,
            scope,
            hashlib.sha256(canonical_request.encode()).hexdigest(),
        )
    )
    key = _hmac(("AWS4" + credentials.secret_access_key).encode(), date_stamp)
    key = _hmac(key, region)
    key = _hmac(key, service)
    key = _hmac(key, "aws4_request")
    signature = hmac.new(key, string_to_sign.encode(), hashlib.sha256).hexdigest()
    result = dict(signed)
    result["authorization"] = (
        "AWS4-HMAC-SHA256 "
        f"Credential={credentials.access_key_id}/{scope}, "
        f"SignedHeaders={signed_names}, Signature={signature}"
    )
    return result


def _hmac(key: bytes, value: str) -> bytes:
    return hmac.new(key, value.encode(), hashlib.sha256).digest()


def _decode_eventstream_headers(data: bytes) -> dict[str, object]:
    headers: dict[str, object] = {}
    offset = 0
    while offset < len(data):
        name_length = data[offset]
        offset += 1
        if offset + name_length + 1 > len(data):
            raise ProviderProtocolError("AWS EventStream header is truncated")
        name = data[offset : offset + name_length].decode("utf-8")
        offset += name_length
        value_type = data[offset]
        offset += 1
        value, offset = _decode_eventstream_value(data, offset, value_type)
        headers[name] = value
    return headers


def _decode_eventstream_value(data: bytes, offset: int, value_type: int) -> tuple[object, int]:
    if value_type == 0:
        return True, offset
    if value_type == 1:
        return False, offset
    sizes = {2: 1, 3: 2, 4: 4, 5: 8, 8: 8, 9: 16}
    if value_type in sizes:
        end = offset + sizes[value_type]
        if end > len(data):
            raise ProviderProtocolError("AWS EventStream header value is truncated")
        return data[offset:end], end
    if value_type in {6, 7}:
        if offset + 2 > len(data):
            raise ProviderProtocolError("AWS EventStream header length is truncated")
        length = struct.unpack(">H", data[offset : offset + 2])[0]
        start = offset + 2
        end = start + length
        if end > len(data):
            raise ProviderProtocolError("AWS EventStream header value is truncated")
        value = data[start:end]
        return (value.decode("utf-8") if value_type == 7 else value), end
    raise ProviderProtocolError("AWS EventStream header type is unsupported")


def _decode_bedrock_event(message: EventStreamMessage) -> dict[str, Any]:
    message_type = message.headers.get(":message-type")
    if message_type in {"error", "exception"}:
        raise ProviderProtocolError("Bedrock EventStream reported an exception")
    event_type = message.headers.get(":event-type")
    if event_type != "chunk":
        raise ProviderProtocolError("Bedrock EventStream event type is unsupported")
    envelope = _decode_json(message.payload)
    encoded = envelope.get("bytes")
    if not isinstance(encoded, str):
        raise ProviderProtocolError("Bedrock EventStream chunk has no bytes")
    try:
        payload = base64.b64decode(encoded, validate=True)
    except ValueError as exc:
        raise ProviderProtocolError("Bedrock EventStream chunk has invalid base64") from exc
    return _decode_json(payload)


def _validate_bedrock_status(response: ClientResponse, payload: dict[str, Any]) -> None:
    if response.status < 200 or response.status >= 300 or "message" in payload and "type" not in payload:
        raise ProviderProtocolError(
            "Bedrock returned an error envelope",
            disposition=_http_failure(response.status),
        )


def _bedrock_result(
    request: InferenceAttemptRequest,
    payload: dict[str, Any],
    usage_units: int | None,
) -> ProviderWireResult:
    return ProviderWireResult(
        payload=payload,
        usage_units=usage_units,
        credential_observation=CredentialHealthObservation(
            credential_slot_id=request.credential_slot_id,
            credential_version=request.credential_version,
            verdict=CredentialHealthVerdict.SUCCESS,
            reason="provider request succeeded",
        ),
    )


def _nonnegative_int(value: object) -> int | None:
    return value if isinstance(value, int) and value >= 0 else None
