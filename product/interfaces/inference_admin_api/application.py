"""Scope-authorized, redacted administration projections."""

from __future__ import annotations

import hmac
import json
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, is_dataclass
from typing import Any, Protocol, cast

from aiohttp import web

from mote.product.inference.admin_model import AdminReadModel

_AUTHORIZER = web.AppKey("inference_admin_authorizer", object)
_READ_MODEL = web.AppKey("inference_admin_read_model", object)
_MUTATIONS = web.AppKey("inference_admin_mutations", object)
_MAX_BODY = 16 * 1024 * 1024


class AdminApiAuthorizer(Protocol):
    async def authorize(self, bearer_token: str, scope: str) -> bool:
        ...


@dataclass(frozen=True, slots=True)
class AdminMutationModel:
    stage_generation: Callable[[bytes, str, str], Awaitable[Mapping[str, Any]]]
    activate_generation: Callable[[str, str], Awaitable[Mapping[str, Any]]] | None = None


class _StaticAuthorizer:
    def __init__(self, token: str) -> None:
        if not token:
            raise ValueError("admin API bearer token is required")
        self._token = token

    async def authorize(self, bearer_token: str, scope: str) -> bool:
        return hmac.compare_digest(bearer_token, self._token)


def _bearer(request: web.Request) -> str:
    value = request.headers.get("Authorization", "")
    return value[7:] if value.startswith("Bearer ") else ""


async def _authorized(request: web.Request, scope: str) -> web.Response | None:
    authorizer = cast(AdminApiAuthorizer, request.app[_AUTHORIZER])
    if await authorizer.authorize(_bearer(request), scope):
        return None
    return web.json_response({"error": "unauthorized"}, status=401)


async def _list(request: web.Request, scope: str, attribute: str) -> web.Response:
    denied = await _authorized(request, scope)
    if denied is not None:
        return denied
    values = await getattr(request.app[_READ_MODEL], attribute)()
    return web.json_response({"schema_version": 1, "items": list(values)})


async def _providers(request: web.Request) -> web.Response:
    return await _list(request, "providers.read", "providers")


async def _credentials(request: web.Request) -> web.Response:
    return await _list(request, "credentials.read_metadata", "credentials")


async def _generations(request: web.Request) -> web.Response:
    return await _list(request, "generations.read", "generations")


async def _readiness(request: web.Request) -> web.Response:
    denied = await _authorized(request, "operations.read")
    if denied is not None:
        return denied
    value = await cast(AdminReadModel, request.app[_READ_MODEL]).readiness()
    return web.json_response({"schema_version": 1, **value})


async def _receipt(request: web.Request) -> web.Response:
    denied = await _authorized(request, "receipts.read")
    if denied is not None:
        return denied
    value = await cast(AdminReadModel, request.app[_READ_MODEL]).receipt(request.match_info["execution_id"])
    if value is None:
        return web.json_response({"error": "not_found"}, status=404)
    if is_dataclass(value) and not isinstance(value, type):
        document = asdict(value)
    elif isinstance(value, Mapping):
        document = dict(value)
    else:
        raise TypeError("admin receipt projection must be a mapping or dataclass")
    return web.json_response({"schema_version": 1, "receipt": document}, dumps=_json_dumps)


async def _reconciliation(request: web.Request) -> web.Response:
    return await _list(request, "reconciliation.read", "reconciliation")


async def _audit(request: web.Request) -> web.Response:
    denied = await _authorized(request, "audit.read")
    if denied is not None:
        return denied
    try:
        after = int(request.query.get("after", "0"))
    except ValueError:
        return web.json_response({"error": "invalid_cursor"}, status=400)
    if after < 0:
        return web.json_response({"error": "invalid_cursor"}, status=400)
    values = await cast(AdminReadModel, request.app[_READ_MODEL]).audit(after)
    return web.json_response({"schema_version": 1, "items": list(values)})


async def _stage_generation(request: web.Request) -> web.Response:
    denied = await _authorized(request, "generations.stage")
    if denied is not None:
        return denied
    mutations = request.app.get(_MUTATIONS)
    if mutations is None:
        return web.json_response({"error": "mutation_not_configured"}, status=503)
    if request.content_length is not None and request.content_length > _MAX_BODY:
        return web.json_response({"error": "request_too_large"}, status=413)
    try:
        body = await request.json()
        if not isinstance(body, dict):
            raise ValueError("request body must be an object")
        generation_id = body["generation_id"]
        artifact_digest = body["artifact_digest"]
        artifact = body["generation_artifact"]
        if not isinstance(generation_id, str) or not generation_id:
            raise ValueError("generation_id is required")
        if not isinstance(artifact_digest, str) or not artifact_digest.startswith("sha256:"):
            raise ValueError("artifact_digest is invalid")
        if not isinstance(artifact, dict):
            raise ValueError("generation_artifact must be an object")
        if artifact.get("generation_id") != generation_id:
            raise ValueError("generation artifact id mismatch")
        if artifact.get("artifact_digest") != artifact_digest:
            raise ValueError("generation artifact digest mismatch")
        encoded = json.dumps(artifact, separators=(",", ":")).encode()
        value = await cast(AdminMutationModel, mutations).stage_generation(encoded, generation_id, artifact_digest)
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        return web.json_response({"error": "invalid_request", "message": str(exc)}, status=400)
    return web.json_response({"schema_version": 1, "generation": dict(value)}, status=202)


async def _activate_generation(request: web.Request) -> web.Response:
    denied = await _authorized(request, "generations.activate")
    if denied is not None:
        return denied
    mutations = cast(AdminMutationModel | None, request.app.get(_MUTATIONS))
    if mutations is None or mutations.activate_generation is None:
        return web.json_response({"error": "mutation_not_configured"}, status=503)
    if request.content_length is not None and request.content_length > _MAX_BODY:
        return web.json_response({"error": "request_too_large"}, status=413)
    try:
        body = await request.json()
        if not isinstance(body, dict):
            raise ValueError("request body must be an object")
        generation_id = body["generation_id"]
        artifact_digest = body["artifact_digest"]
        if not isinstance(generation_id, str) or not generation_id:
            raise ValueError("generation_id is required")
        if (
            not isinstance(artifact_digest, str)
            or len(artifact_digest) != 71
            or not artifact_digest.startswith("sha256:")
        ):
            raise ValueError("artifact_digest is invalid")
        value = await mutations.activate_generation(generation_id, artifact_digest)
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        return web.json_response({"error": "invalid_request", "message": str(exc)}, status=400)
    return web.json_response({"schema_version": 1, "generation": dict(value)}, status=202)


def _json_dumps(value: object) -> str:
    return json.dumps(value, default=str, separators=(",", ":"))


def build_inference_admin_api(
    read_model: AdminReadModel,
    *,
    bearer_token: str | None = None,
    authorizer: AdminApiAuthorizer | None = None,
    mutations: AdminMutationModel | None = None,
) -> web.Application:
    if authorizer is None:
        authorizer = _StaticAuthorizer(bearer_token or "")
    app = web.Application(client_max_size=_MAX_BODY)
    app[_AUTHORIZER] = authorizer
    app[_READ_MODEL] = read_model
    if mutations is not None:
        app[_MUTATIONS] = mutations
    app.router.add_get("/admin/v1/providers", _providers)
    app.router.add_get("/admin/v1/credentials", _credentials)
    app.router.add_get("/admin/v1/generations", _generations)
    app.router.add_get("/admin/v1/readiness", _readiness)
    app.router.add_get("/admin/v1/receipts/{execution_id}", _receipt)
    app.router.add_get("/admin/v1/reconciliation", _reconciliation)
    app.router.add_get("/admin/v1/audit", _audit)
    app.router.add_post("/admin/v1/generations/stage", _stage_generation)
    app.router.add_post("/admin/v1/generations/activate", _activate_generation)
    return app


__all__ = [
    "AdminApiAuthorizer",
    "AdminMutationModel",
    "AdminReadModel",
    "build_inference_admin_api",
]
