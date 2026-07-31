"""Authenticated provider evidence ingress; never an execution owner."""

from __future__ import annotations

import json
from typing import Protocol, cast

from aiohttp import web
from pydantic import ValidationError

from mote.contracts.inference.provider_evidence import ProviderEvidence, ProviderEvidenceConflictError

_VERIFIER = web.AppKey("provider_webhook_verifier", object)
_SINK = web.AppKey("provider_webhook_sink", object)
_MAX_BODY = 1024 * 1024


class ProviderWebhookVerifier(Protocol):
    async def verify(self, provider: str, event_id: str, signature: str, body: bytes) -> bool:
        ...


class ProviderEvidenceSink(Protocol):
    async def append(self, evidence: ProviderEvidence) -> bool:
        ...


async def _receive(request: web.Request) -> web.Response:
    provider = request.match_info["provider"]
    event_id = request.headers.get("X-Provider-Event-Id", "")
    signature = request.headers.get("X-Provider-Signature", "")
    if request.content_length is not None and request.content_length > _MAX_BODY:
        return web.json_response({"error": "request_too_large"}, status=413)
    body = await request.read()
    verifier = cast(ProviderWebhookVerifier, request.app[_VERIFIER])
    if not event_id or not signature or not await verifier.verify(provider, event_id, signature, body):
        return web.json_response({"error": "unauthorized"}, status=401)
    try:
        payload = json.loads(body)
        if not isinstance(payload, dict):
            raise ValueError("provider evidence body must be an object")
        evidence = ProviderEvidence(provider=provider, event_id=event_id, **payload)
    except (TypeError, ValueError, ValidationError, json.JSONDecodeError) as exc:
        return web.json_response({"error": "invalid_evidence", "message": str(exc)}, status=400)
    try:
        inserted = await cast(ProviderEvidenceSink, request.app[_SINK]).append(evidence)
    except ProviderEvidenceConflictError:
        return web.json_response({"error": "evidence_conflict"}, status=409)
    return web.json_response(
        {"schema_version": 1, "event_id": event_id, "accepted": True},
        status=202 if inserted else 200,
    )


def build_inference_webhook_api(verifier: ProviderWebhookVerifier, sink: ProviderEvidenceSink) -> web.Application:
    app = web.Application(client_max_size=_MAX_BODY)
    app[_VERIFIER] = verifier
    app[_SINK] = sink
    app.router.add_post("/v1/webhooks/provider/{provider}", _receive)
    return app


__all__ = [
    "ProviderEvidenceSink",
    "ProviderWebhookVerifier",
    "build_inference_webhook_api",
]
