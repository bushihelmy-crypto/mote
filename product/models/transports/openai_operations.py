"""Retry-free OpenAI batch and file operation transports."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import asdict
from typing import Any
from urllib.parse import quote, urlsplit, urlunsplit

from aiohttp import FormData
from aiohttp.client import ClientTimeout

from mote.contracts.artifact import ArtifactRef
from mote.contracts.inference.executions import BoundExecutionRequest, TransferPartRequest
from mote.contracts.inference.transport import ProviderWireResult
from mote.contracts.ports.inference.provider_transport import WireLifecycleSink
from mote.product.models.transports.artifact_io import ArtifactPublisher, ArtifactResolver
from mote.product.models.transports.connections.aiohttp import AioHttpConnectionLease
from mote.product.models.transports.openai import (
    AuthHeaders,
    ProviderProtocolError,
    _decode_json,
    _read_bounded,
    _validate_status_and_payload,
    _validated_headers,
)


class OpenAIOperationTransport:
    provider = "openai"
    wire_protocol = "openai_v1_operations"

    def __init__(
        self,
        *,
        endpoint_id: str,
        credential_slot_id: str,
        base_url: str,
        connection: AioHttpConnectionLease,
        auth_headers: AuthHeaders,
        artifact_resolver: ArtifactResolver | None = None,
        artifact_publisher: ArtifactPublisher | None = None,
        max_response_bytes: int = 16 * 1024 * 1024,
        max_upload_bytes: int = 32 * 1024 * 1024,
        allow_http_for_testing: bool = False,
    ) -> None:
        if not endpoint_id or not credential_slot_id or max_response_bytes <= 0 or max_upload_bytes <= 0:
            raise ValueError("invalid OpenAI operation transport configuration")
        self.endpoint_id = endpoint_id
        self.credential_slot_id = credential_slot_id
        self._base_url = _v1_url(base_url, allow_http_for_testing=allow_http_for_testing)
        self._connection = connection
        self._auth_headers = auth_headers
        self._artifact_resolver = artifact_resolver
        self._artifact_publisher = artifact_publisher
        self._max_response_bytes = max_response_bytes
        self._max_upload_bytes = max_upload_bytes
        self._closed = False

    async def execute_once(
        self,
        request: BoundExecutionRequest,
        *,
        local_deadline: float,
        lifecycle: WireLifecycleSink,
    ) -> ProviderWireResult:
        if self._closed:
            raise RuntimeError("operation transport is closed")
        if request.endpoint_binding_id != self.endpoint_id:
            raise ValueError("operation request changed endpoint binding")
        if request.credential_slot_id != self.credential_slot_id:
            raise ValueError("operation request changed credential slot")
        remaining = local_deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            raise TimeoutError("provider deadline exceeded before operation")
        if request.operation == "file.upload":
            if not isinstance(request, TransferPartRequest):
                raise TypeError("file upload requires a transfer part request")
            return await self._upload(request, remaining, lifecycle)
        method, path, query, body = _command(request.operation, request.payload)
        headers = {
            **_validated_headers(await self._auth_headers()),
            "Accept": "application/json",
        }
        if body is not None:
            headers["Content-Type"] = "application/json"
        async with self._connection.session.request(
            method,
            self._base_url + path,
            headers=headers,
            params=query,
            json=body,
            timeout=ClientTimeout(total=remaining),
            allow_redirects=False,
            trace_request_ctx={"lifecycle": lifecycle},
        ) as response:
            if request.operation in {
                "file.content",
                "batch.results",
                "video.download",
                "container_file.content",
            }:
                return await self._download(request, response, lifecycle)
            payload = _decode_json(await _read_bounded(response, self._max_response_bytes))
            _validate_status_and_payload(response.status, payload)
            await lifecycle.response_started()
            return ProviderWireResult(payload={"result": payload})

    async def _download(
        self, request: BoundExecutionRequest, response, lifecycle: WireLifecycleSink
    ) -> ProviderWireResult:
        if response.status < 200 or response.status >= 300:
            payload = _decode_json(await _read_bounded(response, self._max_response_bytes))
            _validate_status_and_payload(response.status, payload)
        content_type = response.headers.get("Content-Type", "").split(";", 1)[0]
        if not content_type or content_type in {"application/json", "text/html"}:
            raise ProviderProtocolError("provider content response has unsafe media type")
        content = await _read_binary(response, self._max_response_bytes)
        if self._artifact_publisher is None:
            raise RuntimeError("download artifact publisher is unavailable")
        identifier = (
            request.payload.get("file_id") or request.payload.get("output_file_id") or request.payload.get("video_id")
        )
        filename = f"{identifier}.bin" if isinstance(identifier, str) else "download.bin"
        artifact = await self._artifact_publisher(content, content_type, filename)
        await lifecycle.response_started()
        return ProviderWireResult(payload={"result": {"artifact": asdict(artifact)}})

    async def _upload(
        self,
        request: TransferPartRequest,
        remaining: float,
        lifecycle: WireLifecycleSink,
    ) -> ProviderWireResult:
        if self._artifact_resolver is None:
            raise RuntimeError("file upload artifact resolver is unavailable")
        raw = request.payload.get("artifact")
        if not isinstance(raw, dict):
            raise ValueError("file upload requires an artifact reference")
        try:
            ref = ArtifactRef(**raw)
        except (TypeError, ValueError) as exc:
            raise ValueError("file upload artifact reference is invalid") from exc
        if ref.size > self._max_upload_bytes:
            raise ValueError("file upload exceeds configured limit")
        resolved = await self._artifact_resolver(ref)
        if resolved.ref != ref:
            raise RuntimeError("artifact resolver changed upload identity")
        purpose = request.payload.get("purpose", "assistants")
        if not isinstance(purpose, str) or not purpose:
            raise ValueError("file upload purpose is required")
        form = FormData(quote_fields=True)
        form.add_field("purpose", purpose)
        form.add_field(
            "file",
            resolved.content,
            filename=ref.suggested_name or "upload.bin",
            content_type=ref.mime_type,
        )
        headers = {
            **_validated_headers(await self._auth_headers()),
            "Accept": "application/json",
        }
        async with self._connection.session.post(
            self._base_url + "/files",
            headers=headers,
            data=form,
            timeout=ClientTimeout(total=remaining),
            allow_redirects=False,
            trace_request_ctx={"lifecycle": lifecycle},
        ) as response:
            payload = _decode_json(await _read_bounded(response, self._max_response_bytes))
            _validate_status_and_payload(response.status, payload)
            await lifecycle.response_started()
            return ProviderWireResult(payload={"result": payload})

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        await self._connection.release()


class ProductOperationTransportResolver:
    def __init__(self, transports: Mapping[tuple[str, str], OpenAIOperationTransport]) -> None:
        self._transports = dict(transports)

    def resolve_command(self, request: BoundExecutionRequest) -> OpenAIOperationTransport:
        return self._resolve(request.endpoint_binding_id, request.credential_slot_id)

    def resolve_transfer_part(self, request: TransferPartRequest) -> OpenAIOperationTransport:
        return self._resolve(request.endpoint_binding_id, request.credential_slot_id)

    def _resolve(self, endpoint_id: str, credential_slot_id: str) -> OpenAIOperationTransport:
        try:
            return self._transports[(endpoint_id, credential_slot_id)]
        except KeyError as exc:
            raise LookupError("no operation transport for endpoint and credential slot") from exc


def _command(
    operation: str, payload: Mapping[str, Any]
) -> tuple[str, str, dict[str, str] | None, dict[str, Any] | None]:
    if operation == "batch.create":
        return "POST", "/batches", None, dict(payload)
    if operation == "batch.list":
        return "GET", "/batches", _query(payload), None
    if operation in {"batch.retrieve", "batch.cancel", "batch.delete"}:
        batch_id = _identifier(payload, "batch_id")
        method = {
            "batch.retrieve": "GET",
            "batch.cancel": "POST",
            "batch.delete": "DELETE",
        }[operation]
        suffix = "/cancel" if operation == "batch.cancel" else ""
        return method, f"/batches/{quote(batch_id, safe='')}" + suffix, None, None
    if operation == "batch.results":
        output_file_id = _identifier(payload, "output_file_id")
        return "GET", f"/files/{quote(output_file_id, safe='')}/content", None, None
    if operation == "file.list":
        return "GET", "/files", _query(payload), None
    if operation in {"file.retrieve", "file.delete", "file.content"}:
        file_id = _identifier(payload, "file_id")
        method = "DELETE" if operation == "file.delete" else "GET"
        suffix = "/content" if operation == "file.content" else ""
        return method, f"/files/{quote(file_id, safe='')}" + suffix, None, None
    if operation in {
        "response.retrieve",
        "response.cancel",
        "response.delete",
        "response.input_items",
    }:
        response_id = _identifier(payload, "response_id")
        method = {
            "response.retrieve": "GET",
            "response.cancel": "POST",
            "response.delete": "DELETE",
            "response.input_items": "GET",
        }[operation]
        suffix = {
            "response.retrieve": "",
            "response.cancel": "/cancel",
            "response.delete": "",
            "response.input_items": "/input_items",
        }[operation]
        query = (
            _query({key: value for key, value in payload.items() if key != "response_id"})
            if operation in {"response.retrieve", "response.input_items"}
            else None
        )
        return method, f"/responses/{quote(response_id, safe='')}" + suffix, query, None
    if operation in {
        "video.generate",
        "video.list",
        "video.retrieve",
        "video.delete",
        "video.download",
        "video.remix",
    }:
        if operation == "video.generate":
            return "POST", "/videos", None, dict(payload)
        if operation == "video.list":
            return "GET", "/videos", _query(payload), None
        video_id = _identifier(payload, "video_id")
        if operation == "video.remix":
            body = {key: value for key, value in payload.items() if key != "video_id"}
            return (
                "POST",
                f"/videos/{quote(video_id, safe='')}/remix",
                None,
                body,
            )
        method = "DELETE" if operation == "video.delete" else "GET"
        suffix = "/content" if operation == "video.download" else ""
        return method, f"/videos/{quote(video_id, safe='')}" + suffix, None, None
    if operation in {
        "container.create",
        "container.list",
        "container.retrieve",
        "container.delete",
    }:
        if operation == "container.create":
            return "POST", "/containers", None, dict(payload)
        if operation == "container.list":
            return "GET", "/containers", _query(payload), None
        container_id = _identifier(payload, "container_id")
        method = "DELETE" if operation == "container.delete" else "GET"
        return method, f"/containers/{quote(container_id, safe='')}", None, None
    if operation in {
        "container_file.create",
        "container_file.list",
        "container_file.retrieve",
        "container_file.delete",
        "container_file.content",
    }:
        container_id = _identifier(payload, "container_id")
        base = f"/containers/{quote(container_id, safe='')}/files"
        if operation == "container_file.create":
            return ("POST", base, None, {key: value for key, value in payload.items() if key != "container_id"})
        if operation == "container_file.list":
            return ("GET", base, _query({key: value for key, value in payload.items() if key != "container_id"}), None)
        file_id = _identifier(payload, "file_id")
        method = "DELETE" if operation == "container_file.delete" else "GET"
        suffix = "/content" if operation == "container_file.content" else ""
        return method, f"{base}/{quote(file_id, safe='')}" + suffix, None, None
    raise ValueError(f"unsupported OpenAI operation {operation!r}")


def _query(payload: Mapping[str, Any]) -> dict[str, str]:
    result: dict[str, str] = {}
    for key, value in payload.items():
        if not isinstance(key, str) or not isinstance(value, (str, int)):
            raise ValueError("operation query values must be strings or integers")
        result[key] = str(value)
    return result


def _identifier(payload: Mapping[str, Any], key: str) -> str:
    value = payload.get(key)
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 256
        or any(character.isspace() or ord(character) < 32 for character in value)
    ):
        raise ValueError(f"{key} is invalid")
    return value


def _v1_url(base_url: str, *, allow_http_for_testing: bool = False) -> str:
    split = urlsplit(base_url)
    if (
        split.scheme not in ({"https", "http"} if allow_http_for_testing else {"https"})
        or not split.netloc
        or split.username
        or split.password
    ):
        raise ValueError("provider base URL must be credential-free HTTPS")
    path = split.path.rstrip("/")
    if not path.endswith("/v1"):
        path += "/v1"
    return urlunsplit((split.scheme, split.netloc, path, "", ""))


async def _read_binary(response, limit: int) -> bytes:
    if response.content_length is not None and response.content_length > limit:
        raise ProviderProtocolError("provider content exceeds configured limit")
    content = await response.content.read(limit + 1)
    if len(content) > limit:
        raise ProviderProtocolError("provider content exceeds configured limit")
    return content


__all__ = ["OpenAIOperationTransport", "ProductOperationTransportResolver"]
