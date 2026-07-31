"""Retry-free OpenAI Chat wire transport with strict response validation."""

from __future__ import annotations

import asyncio
import json
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
from mote.contracts.inference.transport import ProviderTransportFailure, ProviderWireResult
from mote.contracts.model.failover import (
    CredentialVerdict,
    ExternalCommitState,
    FailureDisposition,
    FailureDomain,
    FailureReason,
    HealthVerdict,
    HttpCompatibilityClass,
    QuotaObservation,
    ReconcileStrategy,
    Retryability,
    UsageObservation,
)
from mote.contracts.ports.inference.provider_transport import StreamSink, WireLifecycleSink
from mote.product.models.transports.connections.aiohttp import AioHttpConnectionLease
from mote.product.models.transports.translation import translate_openai_chat, translate_openai_chat_stream
from mote.product.models.transports.validator import PrecommitResponseGuard

AuthHeaders = Callable[[], Awaitable[Mapping[str, str]]]


class ProviderProtocolError(ProviderTransportFailure):
    def __init__(
        self,
        message: str,
        *,
        disposition: FailureDisposition | None = None,
        retry_after_seconds: float | None = None,
    ) -> None:
        super().__init__(
            message,
            disposition=disposition or _ambiguous_protocol_failure(),
            retry_after_seconds=retry_after_seconds,
        )


class OpenAIChatTransport:
    def __init__(
        self,
        *,
        base_url: str,
        connection: AioHttpConnectionLease,
        auth_headers: AuthHeaders,
        max_response_bytes: int = 16 * 1024 * 1024,
        max_stream_frame_bytes: int = 4 * 1024 * 1024,
        max_precommit_bytes: int = 1024 * 1024,
        max_precommit_frames: int = 1024,
        max_precommit_seconds: float = 15.0,
    ) -> None:
        self._url = _chat_completions_url(base_url)
        self._connection = connection
        self._auth_headers = auth_headers
        self._max_response_bytes = max_response_bytes
        self._max_stream_frame_bytes = max_stream_frame_bytes
        self._precommit_limits = (
            max_precommit_bytes,
            max_precommit_frames,
            max_precommit_seconds,
        )
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
        body = _openai_chat_body(request, stream=stream is not None)
        timeout = ClientTimeout(total=remaining)
        async with self._connection.session.post(
            self._url,
            headers=headers,
            json=body,
            timeout=timeout,
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
        body = await _read_bounded(response, self._max_response_bytes)
        payload = _decode_json(body)
        _validate_status_and_payload(
            response.status,
            payload,
            retry_after_seconds=_positive_float_header(response.headers.get("retry-after")),
        )
        if not isinstance(payload.get("choices"), list):
            raise ProviderProtocolError("OpenAI response has no choices array")
        await lifecycle.response_started()
        canonical = translate_openai_chat(payload)
        return _transport_result(
            request,
            response,
            canonical.model_dump(mode="json"),
            _usage_units(payload),
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
        terminal = False
        chunks = 0
        observed_chunks: list[dict[str, Any]] = []
        usage_units: int | None = None
        guard = PrecommitResponseGuard(
            max_bytes=self._precommit_limits[0],
            max_frames=self._precommit_limits[1],
            max_seconds=self._precommit_limits[2],
        )
        while True:
            line = await guard.readline(response.content)
            if not line:
                break
            if len(line) > self._max_stream_frame_bytes:
                raise ProviderProtocolError("SSE frame exceeds configured limit")
            stripped = line.strip()
            if not stripped or stripped.startswith(b":"):
                continue
            if not stripped.startswith(b"data:"):
                raise ProviderProtocolError("malformed SSE field")
            data = stripped[5:].strip()
            if data == b"[DONE]":
                terminal = True
                break
            payload = _decode_json(data)
            _validate_status_and_payload(response.status, payload)
            if not isinstance(payload.get("choices"), list):
                raise ProviderProtocolError("OpenAI stream event has no choices array")
            if chunks == 0:
                await lifecycle.response_started()
                guard.commit()
            chunks += 1
            observed_chunks.append(payload)
            observed_usage = _usage_units(payload)
            if observed_usage is not None:
                usage_units = observed_usage
            await stream.emit(payload)
        if not terminal:
            raise ProviderProtocolError("OpenAI stream ended without [DONE]")
        canonical = translate_openai_chat_stream(tuple(observed_chunks))
        return _transport_result(
            request,
            response,
            canonical.model_dump(mode="json"),
            usage_units,
        )


def _chat_completions_url(base_url: str) -> str:
    split = urlsplit(base_url)
    if split.scheme != "https" or not split.netloc or split.username or split.password:
        raise ValueError("provider base URL must be credential-free HTTPS")
    path = split.path.rstrip("/")
    if not path.endswith("/v1"):
        path += "/v1"
    path += "/chat/completions"
    return urlunsplit((split.scheme, split.netloc, path, "", ""))


def _validated_headers(headers: Mapping[str, str]) -> dict[str, str]:
    forbidden = {"connection", "proxy-authorization", "transfer-encoding", "upgrade"}
    normalized = {str(key): str(value) for key, value in headers.items()}
    if any(key.lower() in forbidden for key in normalized):
        raise ValueError("credential binding returned a forbidden hop-by-hop header")
    if "authorization" not in {key.lower() for key in normalized}:
        raise ValueError("credential binding did not provide authorization")
    return normalized


def _openai_chat_body(request: InferenceAttemptRequest, *, stream: bool) -> dict[str, Any]:
    invocation = request.invocation
    canonical = invocation.get("input")
    if isinstance(canonical, dict) and canonical.get("kind") == "generate":
        messages = _openai_canonical_messages(canonical)
        tools = canonical.get("tools") or ()
        invocation = {
            "messages": messages,
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": tool["name"],
                        "description": tool.get("description", ""),
                        "parameters": tool.get("input_schema") or {},
                    },
                }
                for tool in tools
            ],
            "response_format": (
                {"type": "json_schema", "json_schema": canonical["output_schema"]}
                if canonical.get("output_schema") is not None
                else None
            ),
        }
    messages = invocation.get("messages")
    if not isinstance(messages, list):
        raise ValueError("OpenAI chat invocation requires messages list")
    body: dict[str, Any] = {
        "model": request.endpoint.model,
        "messages": messages,
        "stream": stream,
        "max_tokens": request.endpoint.execution_policy.max_output_tokens,
        "temperature": request.endpoint.execution_policy.temperature_micros / 1_000_000,
    }
    for key in (
        "tools",
        "tool_choice",
        "temperature",
        "top_p",
        "max_tokens",
        "response_format",
    ):
        if key in invocation:
            if invocation[key] is not None:
                body[key] = invocation[key]
    return body


def _openai_canonical_messages(canonical: dict[str, Any]) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    system = canonical.get("system_prompt")
    if system:
        messages.append({"role": "system", "content": system})
    for raw in canonical.get("messages") or ():
        if not isinstance(raw, dict):
            raise ValueError("canonical message must be an object")
        message = {key: raw[key] for key in ("role", "content", "name", "tool_call_id") if raw.get(key) is not None}
        calls = raw.get("tool_calls") or ()
        if calls:
            message["tool_calls"] = [
                {
                    "id": call.get("id", ""),
                    "type": "function",
                    "function": {
                        "name": call["name"],
                        "arguments": json.dumps(
                            call.get("arguments") or {},
                            ensure_ascii=False,
                            separators=(",", ":"),
                        ),
                    },
                }
                for call in calls
            ]
        messages.append(message)
    return messages


async def _read_bounded(response: ClientResponse, limit: int) -> bytes:
    content_length = response.content_length
    if content_length is not None and content_length > limit:
        raise ProviderProtocolError("provider response exceeds configured limit")
    body = await response.content.read(limit + 1)
    if len(body) > limit:
        raise ProviderProtocolError("provider response exceeds configured limit")
    return body


def _decode_json(body: bytes) -> dict[str, Any]:
    if body.lstrip().lower().startswith((b"<!doctype html", b"<html")):
        raise ProviderProtocolError("provider returned HTML")
    try:
        payload = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProviderProtocolError("provider returned malformed JSON") from exc
    if not isinstance(payload, dict):
        raise ProviderProtocolError("provider response must be a JSON object")
    return payload


def _validate_status_and_payload(
    status: int,
    payload: dict[str, Any],
    *,
    retry_after_seconds: float | None = None,
) -> None:
    if "error" in payload:
        raise ProviderProtocolError(
            "provider returned an error envelope",
            disposition=_http_failure(status),
            retry_after_seconds=retry_after_seconds,
        )
    if status < 200 or status >= 300:
        raise ProviderProtocolError(
            f"provider returned HTTP status {status}",
            disposition=_http_failure(status),
            retry_after_seconds=retry_after_seconds,
        )


def _ambiguous_protocol_failure() -> FailureDisposition:
    return FailureDisposition(
        reason=FailureReason.RESPONSE_UNPARSABLE,
        domain=FailureDomain.PROTOCOL,
        retryability=Retryability.RECONCILE_ONLY,
        external_commit_state=ExternalCommitState.UNKNOWN,
        safe_message="provider response violated protocol",
        reconcile_strategy=ReconcileStrategy.RECEIPT_WAIT,
        usage_observation=UsageObservation.PENDING_RECONCILIATION,
        http_compatibility_class=HttpCompatibilityClass.INTERNAL,
    )


def _http_failure(status: int) -> FailureDisposition:
    if status in {401, 403}:
        return FailureDisposition(
            reason=FailureReason.AUTH_REJECTED,
            domain=FailureDomain.CREDENTIAL,
            retryability=Retryability.NEW_ATTEMPT,
            credential_verdict=CredentialVerdict.QUARANTINE,
            safe_message="provider rejected credential",
            http_compatibility_class=(
                HttpCompatibilityClass.AUTHENTICATION if status == 401 else HttpCompatibilityClass.PERMISSION
            ),
        )
    if status == 429:
        return FailureDisposition(
            reason=FailureReason.RATE_LIMITED,
            domain=FailureDomain.QUOTA,
            retryability=Retryability.AFTER_HINT,
            quota_observation=QuotaObservation.RETRY_AFTER,
            safe_message="provider quota rejected request",
            http_compatibility_class=HttpCompatibilityClass.QUOTA,
        )
    if status >= 500:
        return FailureDisposition(
            reason=FailureReason.SERVER_ERROR,
            domain=FailureDomain.PROVIDER,
            retryability=Retryability.NEW_ATTEMPT,
            health_verdict=HealthVerdict.DEGRADE,
            safe_message="provider unavailable",
            http_compatibility_class=HttpCompatibilityClass.UNAVAILABLE,
        )
    return FailureDisposition(
        reason=FailureReason.PROTOCOL_INCOMPATIBLE,
        domain=FailureDomain.PROTOCOL,
        retryability=Retryability.NEVER,
        safe_message="provider rejected request",
        http_compatibility_class=HttpCompatibilityClass.INVALID_REQUEST,
    )


def _transport_result(
    request: InferenceAttemptRequest,
    response: ClientResponse,
    payload: dict[str, Any],
    usage_units: int | None,
) -> ProviderWireResult:
    return ProviderWireResult(
        payload=payload,
        usage_units=usage_units,
        quota_observation=_quota_observation(request, response),
        credential_observation=CredentialHealthObservation(
            credential_slot_id=request.credential_slot_id,
            credential_version=request.credential_version,
            verdict=CredentialHealthVerdict.SUCCESS,
            reason="provider request succeeded",
        ),
    )


def _usage_units(payload: dict[str, Any]) -> int | None:
    usage = payload.get("usage")
    if not isinstance(usage, dict):
        return None
    total = usage.get("total_tokens")
    if isinstance(total, int) and total >= 0:
        return total
    prompt = usage.get("prompt_tokens")
    completion = usage.get("completion_tokens")
    if isinstance(prompt, int) and prompt >= 0 and isinstance(completion, int) and completion >= 0:
        return prompt + completion
    return None


def _quota_observation(request: InferenceAttemptRequest, response: ClientResponse) -> ProviderQuotaObservation | None:
    remaining_requests = _nonnegative_int_header(response.headers.get("x-ratelimit-remaining-requests"))
    remaining_tokens = _nonnegative_int_header(response.headers.get("x-ratelimit-remaining-tokens"))
    retry_after = _positive_float_header(response.headers.get("retry-after"))
    if remaining_requests is None and remaining_tokens is None and retry_after is None:
        return None
    return ProviderQuotaObservation(
        provider=request.endpoint.provider,
        endpoint_id=request.endpoint.endpoint_id,
        credential_slot_id=request.credential_slot_id,
        kind=(QuotaObservationKind.RETRY_AFTER if retry_after is not None else QuotaObservationKind.LIMITS),
        remaining_requests=remaining_requests,
        remaining_tokens=remaining_tokens,
        retry_after_seconds=retry_after,
    )


def _nonnegative_int_header(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        parsed = int(value)
    except ValueError:
        return None
    return parsed if parsed >= 0 else None


def _positive_float_header(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        parsed = float(value)
    except ValueError:
        return None
    return parsed if parsed > 0 else None
