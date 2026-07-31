"""Retry-free Gemini/Vertex GenerateContent transport."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from typing import Any
from urllib.parse import quote, urlsplit, urlunsplit

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
from mote.product.models.transports.translation import translate_google_generate_content

AuthHeaders = Callable[[], Awaitable[Mapping[str, str]]]


class GoogleGenerateContentTransport:
    def __init__(
        self,
        *,
        base_url: str,
        connection: AioHttpConnectionLease,
        auth_headers: AuthHeaders,
        max_response_bytes: int = 16 * 1024 * 1024,
        max_stream_frame_bytes: int = 4 * 1024 * 1024,
    ) -> None:
        self._base_url = base_url
        _generate_content_url(base_url, "model", stream=False)
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
        headers = _google_headers(await self._auth_headers())
        async with self._connection.session.post(
            _generate_content_url(
                self._base_url,
                request.endpoint.model,
                stream=stream is not None,
            ),
            headers=headers,
            json=_generate_content_body(request),
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
        _validate_google_status(response, payload)
        _validate_generate_content(payload, require_terminal=True)
        await lifecycle.response_started()
        canonical = translate_google_generate_content((payload,))
        return _google_result(
            request,
            response,
            canonical.model_dump(mode="json"),
            _google_usage(payload),
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
            _validate_google_status(response, payload)
        started = False
        terminal = False
        chunks = 0
        usage_units: int | None = None
        observed_payloads: list[dict[str, Any]] = []
        while True:
            line = await response.content.readline()
            if not line:
                break
            if len(line) > self._max_stream_frame_bytes:
                raise ProviderProtocolError("Google SSE frame exceeds configured limit")
            stripped = line.strip()
            if not stripped or stripped.startswith((b":", b"event:")):
                continue
            if not stripped.startswith(b"data:"):
                raise ProviderProtocolError("malformed Google SSE field")
            payload = _decode_json(stripped[5:].strip())
            _validate_generate_content(payload, require_terminal=False)
            if not started:
                await lifecycle.response_started()
                started = True
            chunks += 1
            observed_payloads.append(payload)
            await stream.emit(payload)
            observed_usage = _google_usage(payload)
            if observed_usage is not None:
                usage_units = observed_usage
            if _has_terminal_candidate(payload):
                terminal = True
                break
        if not terminal:
            raise ProviderProtocolError("Google stream ended without terminal candidate")
        canonical = translate_google_generate_content(tuple(observed_payloads))
        return _google_result(
            request,
            response,
            canonical.model_dump(mode="json"),
            usage_units,
        )


def _generate_content_url(base_url: str, model: str, *, stream: bool) -> str:
    split = urlsplit(base_url)
    if split.scheme != "https" or not split.netloc or split.username or split.password:
        raise ValueError("provider base URL must be credential-free HTTPS")
    path = split.path.rstrip("/")
    operation = "streamGenerateContent" if stream else "generateContent"
    path += f"/models/{quote(model, safe='')}:{operation}"
    query = "alt=sse" if stream else ""
    return urlunsplit((split.scheme, split.netloc, path, query, ""))


def _google_headers(headers: Mapping[str, str]) -> dict[str, str]:
    forbidden = {"connection", "proxy-authorization", "transfer-encoding", "upgrade"}
    normalized = {str(key): str(value) for key, value in headers.items()}
    lowered = {key.lower() for key in normalized}
    if lowered & forbidden:
        raise ValueError("credential binding returned a forbidden hop-by-hop header")
    if "x-goog-api-key" not in lowered and "authorization" not in lowered:
        raise ValueError("credential binding did not provide Google authentication")
    return {
        **normalized,
        "content-type": "application/json",
        "accept": "application/json",
    }


def _generate_content_body(request: InferenceAttemptRequest) -> dict[str, Any]:
    invocation = request.invocation
    canonical = invocation.get("input")
    if isinstance(canonical, dict) and canonical.get("kind") == "generate":
        invocation = {
            "contents": _google_canonical_contents(canonical),
            "systemInstruction": (
                {"parts": [{"text": canonical["system_prompt"]}]} if canonical.get("system_prompt") else None
            ),
            "tools": (
                {
                    "functionDeclarations": [
                        {
                            "name": tool["name"],
                            "description": tool.get("description", ""),
                            "parameters": tool.get("input_schema") or {},
                        }
                        for tool in canonical.get("tools") or ()
                    ]
                }
                if canonical.get("tools")
                else None
            ),
            "generationConfig": {
                "maxOutputTokens": request.endpoint.execution_policy.max_output_tokens,
                "temperature": request.endpoint.execution_policy.temperature_micros / 1_000_000,
            },
        }
    contents = invocation.get("contents")
    if not isinstance(contents, list):
        raise ValueError("GenerateContent invocation requires contents list")
    body: dict[str, Any] = {"contents": contents}
    for key in (
        "systemInstruction",
        "tools",
        "toolConfig",
        "generationConfig",
        "safetySettings",
        "cachedContent",
    ):
        if key in invocation:
            if invocation[key] is not None:
                body[key] = invocation[key]
    return body


def _google_canonical_contents(canonical: dict[str, Any]) -> list[dict[str, Any]]:
    contents: list[dict[str, Any]] = []
    for raw in canonical.get("messages") or ():
        if not isinstance(raw, dict):
            raise ValueError("canonical message must be an object")
        role = raw.get("role")
        content = raw.get("content")
        if role == "tool":
            name = raw.get("name")
            if not isinstance(name, str) or not name:
                raise ValueError("Google canonical tool result requires name")
            response = content if isinstance(content, dict) else {"result": content}
            contents.append(
                {
                    "role": "user",
                    "parts": [{"functionResponse": {"name": name, "response": response}}],
                }
            )
            continue
        if role not in {"user", "assistant"}:
            raise ValueError("Google canonical message role is unsupported")
        if isinstance(content, list):
            parts: list[dict[str, Any]] = list(content)
        elif content in (None, ""):
            parts = []
        else:
            parts = [{"text": str(content)}]
        for call in raw.get("tool_calls") or ():
            parts.append(
                {
                    "functionCall": {
                        "name": call["name"],
                        "args": call.get("arguments") or {},
                    }
                }
            )
        contents.append({"role": "model" if role == "assistant" else "user", "parts": parts})
    return contents


def _validate_google_status(response: ClientResponse, payload: dict[str, Any]) -> None:
    if "error" in payload or response.status < 200 or response.status >= 300:
        raise ProviderProtocolError(
            "Google returned an error envelope",
            disposition=_http_failure(response.status),
            retry_after_seconds=_positive_float_header(response.headers.get("retry-after")),
        )


def _validate_generate_content(payload: dict[str, Any], *, require_terminal: bool) -> None:
    candidates = payload.get("candidates")
    if not isinstance(candidates, list):
        raise ProviderProtocolError("GenerateContent response has no candidates array")
    if require_terminal and not _has_terminal_candidate(payload):
        raise ProviderProtocolError("GenerateContent response has no terminal candidate")


def _has_terminal_candidate(payload: dict[str, Any]) -> bool:
    candidates = payload.get("candidates")
    return isinstance(candidates, list) and any(
        isinstance(candidate, dict) and isinstance(candidate.get("finishReason"), str) for candidate in candidates
    )


def _google_usage(payload: dict[str, Any]) -> int | None:
    usage = payload.get("usageMetadata")
    if not isinstance(usage, dict):
        return None
    total = usage.get("totalTokenCount")
    return total if isinstance(total, int) and total >= 0 else None


def _google_result(
    request: InferenceAttemptRequest,
    response: ClientResponse,
    payload: dict[str, Any],
    usage_units: int | None,
) -> ProviderWireResult:
    return ProviderWireResult(
        payload=payload,
        usage_units=usage_units,
        quota_observation=_google_quota(request, response),
        credential_observation=CredentialHealthObservation(
            credential_slot_id=request.credential_slot_id,
            credential_version=request.credential_version,
            verdict=CredentialHealthVerdict.SUCCESS,
            reason="provider request succeeded",
        ),
    )


def _google_quota(request: InferenceAttemptRequest, response: ClientResponse) -> ProviderQuotaObservation | None:
    requests = _nonnegative_int_header(response.headers.get("x-ratelimit-remaining-requests"))
    tokens = _nonnegative_int_header(response.headers.get("x-ratelimit-remaining-tokens"))
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
