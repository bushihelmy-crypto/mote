"""Authenticated OpenAI-compatible HTTP projection onto one ModelGateway."""

from __future__ import annotations

import asyncio
import hmac
import json
from collections.abc import Awaitable, Callable
from typing import Protocol, cast
from uuid import uuid4

from aiohttp import web
from aiohttp.multipart import BodyPartReader
from pydantic import JsonValue, ValidationError

from mote.contracts.artifact import ArtifactRef, ResolvedArtifact
from mote.contracts.model.invocation import CanonicalMessage, GenerateInput, ModelInvocation, ResolvedModelResponse
from mote.contracts.model.operations import ModelOperation
from mote.contracts.model.topology import DefaultRoute, RouteId
from mote.contracts.ports.model.gateway import ModelGateway
from mote.product.inference.session_gateway import RealtimeSessionOwner
from mote.product.interfaces.inference_api.model_operations import ModelGatewayCompatibilityOwner
from mote.product.interfaces.inference_api.operations import (
    ArtifactCompatibilityOwner,
    DurableCompatibilityOwner,
    UnaryCompatibilityOwner,
)

_MAX_BODY = 16 * 1024 * 1024
_MAX_MULTIPART_FIELDS = 32
_MAX_MULTIPART_FIELD_BYTES = 16 * 1024


class InferenceApiAuthorizer(Protocol):
    async def authorize(self, bearer_token: str, scope: str) -> bool: ...


class DurableResponseOwner(Protocol):
    async def retrieve(self, response_id: str) -> dict | None: ...

    async def cancel(self, response_id: str) -> dict | None: ...

    async def delete(self, response_id: str) -> dict | None: ...

    async def input_items(self, response_id: str, query: dict[str, str]) -> dict | None: ...


ArtifactReader = Callable[[ArtifactRef], Awaitable[ResolvedArtifact]]

_GATEWAY = web.AppKey[ModelGateway]("inference_gateway")
_AUTHORIZER = web.AppKey[InferenceApiAuthorizer]("inference_authorizer")
_ROUTE = web.AppKey[RouteId]("inference_route")
_DURABLE_RESPONSES = web.AppKey[DurableResponseOwner]("inference_durable_responses")
_REALTIME_SESSIONS = web.AppKey[RealtimeSessionOwner]("inference_realtime_sessions")
_UNARY_OPERATIONS = web.AppKey[UnaryCompatibilityOwner]("inference_unary_operations")
_DURABLE_OPERATIONS = web.AppKey[DurableCompatibilityOwner]("inference_durable_operations")
_ARTIFACT_OPERATIONS = web.AppKey[ArtifactCompatibilityOwner]("inference_artifact_operations")
_ARTIFACT_READER = web.AppKey[ArtifactReader]("inference_artifact_reader")


class _StaticBearerAuthorizer:
    def __init__(self, token: str) -> None:
        if not token:
            raise ValueError("inference API bearer token is required")
        self._token = token

    async def authorize(self, bearer_token: str, scope: str) -> bool:
        return hmac.compare_digest(bearer_token, self._token)


async def _error(error_type: str, message: str, status: int) -> web.Response:
    return web.json_response(
        {"error": {"type": error_type, "code": None, "message": message}},
        status=status,
    )


def _bearer(request: web.Request) -> str:
    header = request.headers.get("Authorization", "")
    return header[7:] if header.startswith("Bearer ") else ""


async def _require(request: web.Request, scope: str) -> web.Response | None:
    authorizer = request.app[_AUTHORIZER]
    if not await authorizer.authorize(_bearer(request), scope):
        return await _error("authentication_error", "unauthorized", 401)
    return None


async def _models(request: web.Request) -> web.Response:
    denied = await _require(request, "models.read")
    if denied is not None:
        return denied
    gateway = request.app[_GATEWAY]
    route = request.app[_ROUTE]
    profiles = gateway.route_profiles(route)
    return web.json_response(
        {
            "object": "list",
            "data": [{"id": item.model, "object": "model", "owned_by": item.provider} for item in profiles],
        }
    )


async def _generate(request: web.Request) -> web.Response:
    denied = await _require(request, "inference.execute")
    if denied is not None:
        return denied
    if request.content_length is not None and request.content_length > _MAX_BODY:
        return await _error("invalid_request_error", "request body is too large", 413)
    try:
        body = await request.json()
        if not isinstance(body, dict):
            raise ValueError("request body must be an object")
        if body.get("stream") is True:
            raise ValueError("streaming compatibility is not available on this route")
        raw_messages = body.get("messages")
        if not isinstance(raw_messages, list) or not raw_messages:
            raise ValueError("messages must be a non-empty array")
        messages = tuple(
            CanonicalMessage(role=item["role"], content=item.get("content", ""))
            for item in raw_messages
            if isinstance(item, dict)
        )
        if len(messages) != len(raw_messages):
            raise ValueError("every message must be an object")
        call_id = str(body.get("request_id") or uuid4())
        invocation = ModelInvocation(
            model_call_id=call_id,
            route_id=request.app[_ROUTE],
            task="compatibility.chat.completions",
            operation=ModelOperation.GENERATE,
            input=GenerateInput(messages=messages),
        )
        result = await request.app[_GATEWAY].execute(invocation, stream=False)
    except (KeyError, ValueError, ValidationError) as exc:
        return await _error("invalid_request_error", str(exc), 400)
    try:
        return _chat_response(result)
    except RuntimeError:
        return await _error("internal_error", "gateway returned an incompatible response", 500)


async def _responses(request: web.Request) -> web.Response:
    denied = await _require(request, "inference.execute")
    if denied is not None:
        return denied
    if request.content_length is not None and request.content_length > _MAX_BODY:
        return await _error("invalid_request_error", "request body is too large", 413)
    try:
        body = await request.json()
        if not isinstance(body, dict):
            raise ValueError("request body must be an object")
        if body.get("stream") is True:
            raise ValueError("streaming compatibility is not available on this route")
        messages = _responses_messages(body.get("input"))
        instructions = body.get("instructions", "")
        if not isinstance(instructions, str):
            raise ValueError("instructions must be a string")
        call_id = str(body.get("request_id") or uuid4())
        invocation = ModelInvocation(
            model_call_id=call_id,
            route_id=request.app[_ROUTE],
            task="compatibility.responses",
            operation=ModelOperation.GENERATE,
            input=GenerateInput(messages=messages, system_prompt=instructions),
        )
        result = await request.app[_GATEWAY].execute(invocation, stream=False)
    except (KeyError, ValueError, ValidationError) as exc:
        return await _error("invalid_request_error", str(exc), 400)
    output = result.output
    if output.kind != "generate":
        return await _error("internal_error", "gateway returned an incompatible response", 500)
    return web.json_response(
        {
            "id": result.model_call_id,
            "object": "response",
            "status": "completed",
            "model": result.model_or_deployment,
            "output": [
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": output.content}],
                }
            ],
            "usage": {
                "input_tokens": result.usage.input_tokens,
                "output_tokens": result.usage.output_tokens,
                "total_tokens": result.usage.total_tokens,
            },
        }
    )


def _responses_messages(value: object) -> tuple[CanonicalMessage, ...]:
    if isinstance(value, str):
        return (CanonicalMessage(role="user", content=value),)
    if not isinstance(value, list) or not value:
        raise ValueError("input must be a non-empty string or array")
    messages: list[CanonicalMessage] = []
    for item in value:
        if not isinstance(item, dict):
            raise ValueError("every input item must be an object")
        role = item.get("role")
        if not isinstance(role, str) or not role:
            raise ValueError("response input role must be a non-empty string")
        content = item.get("content", "")
        if isinstance(content, list):
            text_parts = []
            for part in content:
                if not isinstance(part, dict) or part.get("type") not in {
                    "input_text",
                    "text",
                }:
                    raise ValueError("only text response input content is supported")
                text = part.get("text")
                if not isinstance(text, str):
                    raise ValueError("response input text must be a string")
                text_parts.append(text)
            content = "".join(text_parts)
        messages.append(CanonicalMessage(role=role, content=content))
    return tuple(messages)


async def _response_lifecycle(request: web.Request, operation: str) -> web.Response:
    denied = await _require(request, "responses.manage")
    if denied is not None:
        return denied
    owner = request.app.get(_DURABLE_RESPONSES)
    if owner is None:
        return await _error("service_unavailable", "durable response owner is not configured", 503)
    response_id = request.match_info["response_id"]
    value = await getattr(owner, operation)(response_id)
    if value is None:
        return await _error("invalid_request_error", "response was not found", 404)
    return web.json_response(value)


async def _retrieve_response(request: web.Request) -> web.Response:
    return await _response_lifecycle(request, "retrieve")


async def _cancel_response(request: web.Request) -> web.Response:
    return await _response_lifecycle(request, "cancel")


async def _delete_response(request: web.Request) -> web.Response:
    return await _response_lifecycle(request, "delete")


async def _response_input_items(request: web.Request) -> web.Response:
    denied = await _require(request, "responses.manage")
    if denied is not None:
        return denied
    owner = request.app.get(_DURABLE_RESPONSES)
    if owner is None:
        return await _error("service_unavailable", "durable response owner is not configured", 503)
    value = await owner.input_items(request.match_info["response_id"], dict(request.query))
    if value is None:
        return await _error("invalid_request_error", "response was not found", 404)
    return web.json_response(value)


async def _realtime(request: web.Request) -> web.StreamResponse:
    denied = await _require(request, "realtime.execute")
    if denied is not None:
        return denied
    owner = request.app.get(_REALTIME_SESSIONS)
    if owner is None:
        return await _error("service_unavailable", "realtime session owner is unavailable", 503)
    model = request.query.get("model", "")
    if not model:
        return await _error("invalid_request_error", "model is required", 400)
    try:
        session = await owner.open({"model": model})
    except (RuntimeError, ValueError) as exc:
        return await _error("invalid_request_error", str(exc), 400)
    socket = web.WebSocketResponse(max_msg_size=_MAX_BODY)
    await socket.prepare(request)

    async def send_events() -> None:
        async for event in session:
            await socket.send_json(
                {
                    "type": event.event_type.value,
                    "session_id": event.session_id,
                    "sequence": event.sequence,
                    "receipt_revision": event.receipt_revision,
                    "payload": event.payload,
                }
            )
            if event.terminal:
                return

    sender = asyncio.create_task(send_events(), name="realtime-websocket-events")
    close_reason = "client disconnected"
    try:
        iterator = socket.__aiter__()
        while True:
            incoming = asyncio.create_task(anext(iterator), name="realtime-websocket-receive")
            done, _pending = await asyncio.wait({incoming, sender}, return_when=asyncio.FIRST_COMPLETED)
            if sender in done:
                incoming.cancel()
                await asyncio.gather(incoming, return_exceptions=True)
                error = sender.exception()
                if error is not None:
                    raise RuntimeError("realtime event stream failed") from error
                break
            try:
                message = incoming.result()
            except StopAsyncIteration:
                break
            if message.type is web.WSMsgType.TEXT:
                try:
                    document = json.loads(message.data)
                except json.JSONDecodeError as exc:
                    raise ValueError("realtime message must be valid JSON") from exc
                if not isinstance(document, dict):
                    raise ValueError("realtime message must be an object")
                sequence = document.get("sequence")
                message_type = document.get("type")
                if not isinstance(sequence, int) or not isinstance(message_type, str):
                    raise ValueError("realtime message requires type and sequence")
                payload = {key: value for key, value in document.items() if key not in {"type", "sequence"}}
                await session.send(sequence=sequence, message_type=message_type, payload=payload)
            elif message.type is web.WSMsgType.ERROR:
                close_reason = "websocket transport error"
                break
    except (RuntimeError, TypeError, ValueError) as exc:
        close_reason = str(exc)
        await socket.close(code=1008, message=str(exc).encode()[:123])
    finally:
        await session.close(close_reason)
        sender.cancel()
        await asyncio.gather(sender, return_exceptions=True)
    return socket


async def _json_body(request: web.Request) -> dict[str, JsonValue]:
    if request.content_length is not None and request.content_length > _MAX_BODY:
        raise OverflowError("request body is too large")
    document = await request.json()
    if not isinstance(document, dict):
        raise ValueError("request body must be an object")
    return cast(dict[str, JsonValue], document)


async def _unary_operation(request: web.Request, operation: str) -> web.Response:
    denied = await _require(request, "inference.execute")
    if denied is not None:
        return denied
    owner = request.app.get(_UNARY_OPERATIONS)
    if owner is None:
        return await _error("service_unavailable", f"{operation} owner is unavailable", 503)
    try:
        payload = await _json_body(request)
        if payload.get("stream") is True:
            raise ValueError("streaming compatibility is not available on this route")
        result = await owner.execute(operation, payload)
    except OverflowError as exc:
        return await _error("invalid_request_error", str(exc), 413)
    except (TypeError, ValueError, ValidationError) as exc:
        return await _error("invalid_request_error", str(exc), 400)
    return web.json_response(result)


async def _embeddings(request: web.Request) -> web.Response:
    return await _unary_operation(request, "embeddings.create")


async def _images(request: web.Request) -> web.Response:
    return await _unary_operation(request, "images.generate")


async def _audio_speech(request: web.Request) -> web.Response:
    return await _unary_operation(request, "audio.speech")


async def _audio_transcriptions(request: web.Request) -> web.Response:
    return await _unary_operation(request, "audio.transcriptions")


async def _durable_operation(request: web.Request, operation: str) -> web.Response:
    denied = await _require(request, f"{operation}.execute")
    if denied is not None:
        return denied
    owner = request.app.get(_DURABLE_OPERATIONS)
    if owner is None:
        return await _error("service_unavailable", f"{operation} owner is unavailable", 503)
    try:
        result = await owner.execute(operation, await _json_body(request))
    except OverflowError as exc:
        return await _error("invalid_request_error", str(exc), 413)
    except (TypeError, ValueError, ValidationError) as exc:
        return await _error("invalid_request_error", str(exc), 400)
    return web.json_response(result)


async def _list_durable_operation(request: web.Request, operation: str) -> web.Response:
    denied = await _require(request, f"{operation}.read")
    if denied is not None:
        return denied
    owner = request.app.get(_DURABLE_OPERATIONS)
    if owner is None:
        return await _error("service_unavailable", f"{operation} owner is unavailable", 503)
    return web.json_response(await owner.list(operation, dict(request.query)))


async def _files_upload(request: web.Request) -> web.Response:
    denied = await _require(request, "files.execute")
    if denied is not None:
        return denied
    owner = request.app.get(_ARTIFACT_OPERATIONS)
    if owner is None:
        return await _error("service_unavailable", "files owner is unavailable", 503)
    try:
        if request.content_type.startswith("multipart/"):
            content, filename, content_type, fields = await _multipart_file(request)
            result = await owner.upload_bytes(
                "files.upload",
                content,
                filename=filename,
                content_type=content_type,
                fields=fields,
            )
        else:
            result = await owner.upload("files.upload", await _json_body(request))
    except OverflowError as exc:
        return await _error("invalid_request_error", str(exc), 413)
    except (TypeError, ValueError, ValidationError) as exc:
        return await _error("invalid_request_error", str(exc), 400)
    return web.json_response(result)


async def _multipart_file(
    request: web.Request,
) -> tuple[bytes, str, str, dict[str, str]]:
    if request.content_length is not None and request.content_length > _MAX_BODY:
        raise OverflowError("request body is too large")
    reader = await request.multipart()
    content: bytes | None = None
    filename = ""
    content_type = "application/octet-stream"
    fields: dict[str, str] = {}
    part_count = 0
    while True:
        candidate = await reader.next()
        if candidate is not None and not isinstance(candidate, BodyPartReader):
            raise ValueError("nested multipart parts are not supported")
        part = candidate
        if part is None:
            break
        part_count += 1
        if part_count > _MAX_MULTIPART_FIELDS:
            raise ValueError("multipart request has too many parts")
        if part.name == "file":
            if content is not None:
                raise ValueError("multipart request must contain exactly one file")
            chunks: list[bytes] = []
            size = 0
            while True:
                chunk = await part.read_chunk()
                if not chunk:
                    break
                size += len(chunk)
                if size > _MAX_BODY:
                    raise OverflowError("uploaded file is too large")
                chunks.append(chunk)
            content = b"".join(chunks)
            filename = part.filename or "upload.bin"
            content_type = part.headers.get("Content-Type", content_type)
            continue
        if not part.name or part.filename is not None:
            raise ValueError("multipart request contains an unsupported part")
        raw = await part.read(decode=True)
        if len(raw) > _MAX_MULTIPART_FIELD_BYTES:
            raise ValueError("multipart field is too large")
        try:
            fields[part.name] = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("multipart fields must be UTF-8") from exc
    if content is None:
        raise ValueError("multipart request requires a file part")
    return content, filename, content_type, fields


async def _files_list(request: web.Request) -> web.Response:
    return await _list_durable_operation(request, "files")


async def _batches_create(request: web.Request) -> web.Response:
    return await _durable_operation(request, "batches")


async def _batches_list(request: web.Request) -> web.Response:
    return await _list_durable_operation(request, "batches")


async def _videos_create(request: web.Request) -> web.Response:
    return await _durable_operation(request, "videos")


async def _videos_list(request: web.Request) -> web.Response:
    return await _list_durable_operation(request, "videos")


async def _containers_create(request: web.Request) -> web.Response:
    return await _durable_operation(request, "containers")


async def _containers_list(request: web.Request) -> web.Response:
    return await _list_durable_operation(request, "containers")


async def _resource_operation(request: web.Request, operation: str) -> web.Response:
    denied = await _require(request, f"{operation}.execute")
    if denied is not None:
        return denied
    owner = request.app.get(_DURABLE_OPERATIONS)
    if owner is None:
        return await _error("service_unavailable", f"{operation} owner is unavailable", 503)
    resource_id = request.match_info["resource_id"]
    try:
        return web.json_response(await owner.resource(operation, resource_id))
    except (TypeError, ValueError, ValidationError) as exc:
        return await _error("invalid_request_error", str(exc), 400)


async def _batch_retrieve(request: web.Request) -> web.Response:
    return await _resource_operation(request, "batch.retrieve")


async def _batch_cancel(request: web.Request) -> web.Response:
    return await _resource_operation(request, "batch.cancel")


async def _batch_delete(request: web.Request) -> web.Response:
    return await _resource_operation(request, "batch.delete")


async def _file_retrieve(request: web.Request) -> web.Response:
    return await _resource_operation(request, "file.retrieve")


async def _file_delete(request: web.Request) -> web.Response:
    return await _resource_operation(request, "file.delete")


async def _video_retrieve(request: web.Request) -> web.Response:
    return await _resource_operation(request, "video.retrieve")


async def _video_delete(request: web.Request) -> web.Response:
    return await _resource_operation(request, "video.delete")


async def _video_remix(request: web.Request) -> web.Response:
    return await _resource_operation(request, "video.remix")


async def _container_retrieve(request: web.Request) -> web.Response:
    return await _resource_operation(request, "container.retrieve")


async def _container_delete(request: web.Request) -> web.Response:
    return await _resource_operation(request, "container.delete")


async def _file_content(request: web.Request) -> web.Response:
    denied = await _require(request, "file.content.execute")
    if denied is not None:
        return denied
    owner = request.app.get(_DURABLE_OPERATIONS)
    reader = request.app.get(_ARTIFACT_READER)
    if owner is None or reader is None:
        return await _error("service_unavailable", "file content owner is unavailable", 503)
    try:
        ref = await owner.content(request.match_info["resource_id"])
        resolved = await reader(ref)
    except (TypeError, ValueError, ValidationError) as exc:
        return await _error("invalid_request_error", str(exc), 400)
    if resolved.ref != ref:
        return await _error("internal_error", "artifact reader changed content identity", 500)
    return web.Response(
        body=resolved.content,
        content_type=ref.mime_type,
        headers={
            "Content-Disposition": f'attachment; filename="{ref.suggested_name or ref.artifact_id}"',
            "ETag": f'"{ref.digest}"',
        },
    )


def _chat_response(result: ResolvedModelResponse) -> web.Response:
    output = result.output
    if output.kind != "generate":
        raise RuntimeError("model gateway returned a non-generate response")
    return web.json_response(
        {
            "id": result.model_call_id,
            "object": "chat.completion",
            "model": result.model_or_deployment,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": output.content},
                    "finish_reason": "stop",
                }
            ],
            "usage": {
                "prompt_tokens": result.usage.input_tokens,
                "completion_tokens": result.usage.output_tokens,
                "total_tokens": result.usage.total_tokens,
            },
        }
    )


def build_inference_api(
    gateway: ModelGateway,
    *,
    bearer_token: str | None = None,
    authorizer: InferenceApiAuthorizer | None = None,
    route_id: RouteId | None = None,
    durable_responses: DurableResponseOwner | None = None,
    realtime_sessions: RealtimeSessionOwner | None = None,
    unary_operations: UnaryCompatibilityOwner | None = None,
    durable_operations: DurableCompatibilityOwner | None = None,
    artifact_operations: ArtifactCompatibilityOwner | None = None,
    artifact_reader: ArtifactReader | None = None,
) -> web.Application:
    if authorizer is None:
        authorizer = _StaticBearerAuthorizer(bearer_token or "")
    app = web.Application(client_max_size=_MAX_BODY)
    app[_GATEWAY] = gateway
    app[_AUTHORIZER] = authorizer
    app[_ROUTE] = route_id or DefaultRoute()
    if unary_operations is None:
        unary_operations = ModelGatewayCompatibilityOwner(gateway, route_id=app[_ROUTE])
    if durable_responses is not None:
        app[_DURABLE_RESPONSES] = durable_responses
    if realtime_sessions is not None:
        app[_REALTIME_SESSIONS] = realtime_sessions
    if unary_operations is not None:
        app[_UNARY_OPERATIONS] = unary_operations
    if durable_operations is not None:
        app[_DURABLE_OPERATIONS] = durable_operations
    if artifact_operations is not None:
        app[_ARTIFACT_OPERATIONS] = artifact_operations
    if artifact_reader is not None:
        app[_ARTIFACT_READER] = artifact_reader
    app.router.add_get("/v1/models", _models)
    app.router.add_post("/v1/chat/completions", _generate)
    app.router.add_post("/v1/responses", _responses)
    app.router.add_get("/v1/responses/{response_id}", _retrieve_response)
    app.router.add_delete("/v1/responses/{response_id}", _delete_response)
    app.router.add_post("/v1/responses/{response_id}/cancel", _cancel_response)
    app.router.add_get("/v1/responses/{response_id}/input_items", _response_input_items)
    app.router.add_get("/v1/realtime", _realtime)
    app.router.add_post("/v1/embeddings", _embeddings)
    app.router.add_post("/v1/images/generations", _images)
    app.router.add_post("/v1/audio/speech", _audio_speech)
    app.router.add_post("/v1/audio/transcriptions", _audio_transcriptions)
    app.router.add_get("/v1/files", _files_list)
    app.router.add_post("/v1/files", _files_upload)
    app.router.add_get("/v1/batches", _batches_list)
    app.router.add_post("/v1/batches", _batches_create)
    app.router.add_get("/v1/batches/{resource_id}", _batch_retrieve)
    app.router.add_post("/v1/batches/{resource_id}/cancel", _batch_cancel)
    app.router.add_delete("/v1/batches/{resource_id}", _batch_delete)
    app.router.add_get("/v1/files/{resource_id}/content", _file_content)
    app.router.add_get("/v1/files/{resource_id}", _file_retrieve)
    app.router.add_delete("/v1/files/{resource_id}", _file_delete)
    app.router.add_get("/v1/videos", _videos_list)
    app.router.add_post("/v1/videos", _videos_create)
    app.router.add_get("/v1/videos/{resource_id}", _video_retrieve)
    app.router.add_delete("/v1/videos/{resource_id}", _video_delete)
    app.router.add_post("/v1/videos/{resource_id}/remix", _video_remix)
    app.router.add_get("/v1/containers", _containers_list)
    app.router.add_post("/v1/containers", _containers_create)
    app.router.add_get("/v1/containers/{resource_id}", _container_retrieve)
    app.router.add_delete("/v1/containers/{resource_id}", _container_delete)
    return app


__all__ = [
    "DurableResponseOwner",
    "InferenceApiAuthorizer",
    "build_inference_api",
]
