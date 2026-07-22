#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""AG-UI SSE server — ``aiohttp`` transport driving one turn per ``/run`` (Phase 2).

The network host for AG-UI / CopilotKit v2. It owns ONLY the socket + routing;
every other concern is borrowed from an existing, unit-tested layer:

* **multi-session** — a shared :class:`SessionRegistry` keeps ``threadId →
  {control, role}`` resident across turns; each request pulls its session from it.
* **per-turn presentation** — a :class:`ConnectionScope` wraps a fresh
  :class:`AguiConsumer` + :class:`AguiPort` over that session, subscribes its own
  projector to the role bus (so concurrent requests never interleave streams),
  drives one turn, and tears the edge down on close.
* **wire shapes** — the pure ``_wire/agui`` mapper (via the consumer).

Endpoints (AG-UI vocabulary): ``POST /agent/{id}/run`` (→ SSE turn stream),
``GET /info`` (discovery), ``POST /connect`` (load thread state), ``POST
/stop/{tid}`` (evict a resident session), ``POST /respond`` (HITL back-channel —
resolve an approval/question raised mid-stream, keyed by its ``promptId``).

Security (§六 publish floor): binds ``127.0.0.1`` by default and REQUIRES a
bearer token — an unauthenticated network endpoint is an open agent-execution
surface. Pass ``token=None`` only together with ``insecure=True`` (explicit
opt-out, logged loudly).
"""

from __future__ import annotations

import asyncio
import hmac
import uuid
from typing import Any, Awaitable, Callable, Optional

from aiohttp import web

from mote.cli import backend
from mote.cli.consumers._wire import agui
from mote.cli.consumers.agui.consumer import AguiConsumer
from mote.cli.consumers.agui.port import AguiPort
from mote.cli.serving import ConnectionScope, PromptBroker, SessionRegistry
from mote.common.logs import logger

# aiohttp app keys (typed access to app-scoped singletons).
_REGISTRY_KEY = web.AppKey("session_registry", SessionRegistry)
_AUTH_KEY = web.AppKey("auth_token", object)
_BROKER_KEY = web.AppKey("prompt_broker", PromptBroker)


def _new_run_id() -> str:
    return f"run-{uuid.uuid4().hex[:12]}"


def _extract_text(body: Any) -> str:
    """Pull the turn's user text from an AG-UI /run body.

    AG-UI posts a ``{threadId, runId?, messages:[...], ...}`` envelope; the turn's
    prompt is the last ``role=='user'`` message's ``content``. Tolerant of a bare
    ``{"message": "..."}`` or ``{"content": "..."}`` shorthand for simple clients.
    """
    if not isinstance(body, dict):
        return ""
    messages = body.get("messages")
    if isinstance(messages, list):
        for msg in reversed(messages):
            if isinstance(msg, dict) and msg.get("role") == "user":
                content = msg.get("content")
                if isinstance(content, str):
                    return content
    for key in ("message", "content", "input", "text"):
        val = body.get(key)
        if isinstance(val, str):
            return val
    return ""


# ── auth ────────────────────────────────────────────────────────────────────
@web.middleware
async def _auth_middleware(request: web.Request, handler: Callable[[web.Request], Awaitable[web.StreamResponse]]):
    """Reject any request missing the configured bearer token.

    The token lives in the app under ``_AUTH_KEY``; ``None`` means an explicit
    ``--insecure`` opt-out (the app factory logs it). We compare with
    ``hmac.compare_digest`` semantics via a constant-ish check to avoid trivial
    timing oracles; the token is a shared secret, not a password hash.
    """
    token = request.app.get(_AUTH_KEY)
    if token is None:  # insecure mode — no gate (logged at startup)
        return await handler(request)
    provided = request.headers.get("Authorization", "")
    if provided.startswith("Bearer "):
        provided = provided[len("Bearer ") :]
    if not _tokens_match(provided, str(token)):
        return web.json_response({"error": "unauthorized"}, status=401)
    return await handler(request)


def _tokens_match(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


# ── handlers ──────────────────────────────────────────────────────────────
async def _handle_info(request: web.Request) -> web.StreamResponse:
    """``GET /info`` — AG-UI discovery: advertise this agent + its capabilities."""
    return web.json_response(
        {
            "name": "mote",
            "description": "mote agent over AG-UI",
            "version": "1",
            "capabilities": {
                "streaming": True,
                "markdown": True,
                "interactive": True,
                "images": True,
            },
            "activeThreads": request.app[_REGISTRY_KEY].session_ids,
        }
    )


async def _handle_connect(request: web.Request) -> web.StreamResponse:
    """``POST /connect`` — ensure a thread is resident, return a state snapshot.

    Idempotent load: mints the session (resuming a persisted rollout when the
    ``threadId`` names one) so a subsequent ``/run`` reuses it. Returns the
    (currently minimal) thread state envelope AG-UI clients expect on load.
    """
    body = await _read_json(request)
    thread_id = body.get("threadId") if isinstance(body, dict) else None
    registry = request.app[_REGISTRY_KEY]
    session = await registry.get_or_create(thread_id)
    return web.json_response({"threadId": session.session_id, "state": {}, "messages": []})


async def _handle_stop(request: web.Request) -> web.StreamResponse:
    """``POST /stop/{tid}`` — evict a resident session (tear down its engine)."""
    thread_id = request.match_info.get("tid", "")
    existed = await request.app[_REGISTRY_KEY].evict(thread_id)
    return web.json_response({"threadId": thread_id, "stopped": existed})


async def _handle_respond(request: web.Request) -> web.StreamResponse:
    """``POST /respond`` — the HITL back-channel that resolves a blocked prompt.

    While a ``/run`` turn streams, a gated tool call emits an ``approval`` /
    ``question`` CUSTOM frame carrying a ``promptId`` and blocks. The frontend
    answers here with ``{promptId, ...}``:

    * approval → ``{promptId, outcome: accept|reject|always_allow|always_deny,
      editedArgs?}``
    * free-text question → ``{promptId, answer}``
    * structured question → ``{promptId, answers:[{header,question,selected,
      free_text}]}``

    The whole body (minus ``promptId``) is handed opaquely to the broker; the
    waiting :class:`AguiPort` maps it to the typed decision/answers. Returns
    ``{resolved: bool}`` — ``false`` when no such prompt is pending (stale retry
    / wrong id), so the client can distinguish a no-op from success.
    """
    body = await _read_json(request)
    prompt_id = body.get("promptId") if isinstance(body, dict) else None
    if not prompt_id:
        return web.json_response({"error": "promptId required"}, status=400)
    resolved = request.app[_BROKER_KEY].resolve(prompt_id, body)
    return web.json_response({"resolved": resolved})


async def _read_json(request: web.Request) -> Any:
    try:
        return await request.json()
    except Exception:  # noqa: BLE001 — a malformed / empty body is just no-input
        return {}


async def _handle_run(request: web.Request) -> web.StreamResponse:
    """``POST /agent/{id}/run`` — drive ONE turn, streaming AG-UI events as SSE.

    The lifecycle, end to end:

    1. Resolve the session from the registry by ``threadId`` (mint / resume on
       first touch); the ``{id}`` path segment is the agent name, informational.
    2. Open a per-request :class:`ConnectionScope` binding a fresh
       :class:`AguiConsumer` (its sink = this response's SSE writer) + an
       :class:`AguiPort` (carries the request's user text) over that session.
    3. Emit ``RUN_STARTED``, drive ``scope.run_turn`` (events fan out through the
       scope's own projector → the consumer → SSE), emit ``RUN_FINISHED``.
    4. Close the scope (unsubscribe + aclose the consumer); the engine stays
       resident in the registry for the next turn.

    The SSE response is a chunked ``text/event-stream``; each frame is
    ``agui.encode_sse(event)``.
    """
    body = await _read_json(request)
    thread_id = body.get("threadId") if isinstance(body, dict) else None
    run_id = (body.get("runId") if isinstance(body, dict) else None) or _new_run_id()
    text = _extract_text(body)

    registry = request.app[_REGISTRY_KEY]
    session = await registry.get_or_create(thread_id)

    response = web.StreamResponse(
        status=200,
        headers={
            "Content-Type": "text/event-stream",
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # disable proxy buffering so frames flush live
        },
    )
    await response.prepare(request)

    async def sink(wire_event: dict) -> None:
        await response.write(agui.encode_sse(wire_event).encode("utf-8"))

    consumer = AguiConsumer(thread_id=session.session_id, run_id=run_id, sink=sink)
    # The port shares the SSE sink + the app-scoped broker so a gated tool call
    # raised during THIS turn streams its approval/question frame down this very
    # stream and blocks on a back-channel ``POST /respond`` keyed by a minted id.
    broker = request.app[_BROKER_KEY]
    port = AguiPort(text, sink=sink, broker=broker, thread_id=session.session_id, run_id=run_id)
    scope = ConnectionScope(session, consumers=[consumer], port=port)

    try:
        async with scope:
            await consumer.emit_lifecycle(agui.run_started(consumer.wire_state))
            message = backend.turn_message(text)
            await scope.run_turn(message)
            await consumer.emit_lifecycle(agui.run_finished(consumer.wire_state))
    except asyncio.CancelledError:  # client disconnected mid-stream
        logger.info(f"AG-UI run {run_id} cancelled (client disconnect)")
        raise
    except Exception as exc:  # noqa: BLE001 — surface any turn failure as RUN_ERROR
        logger.warning(f"AG-UI run {run_id} failed: {exc}")
        try:
            await response.write(agui.encode_sse({"type": agui.RUN_ERROR, "message": str(exc)}).encode("utf-8"))
        except Exception:  # noqa: BLE001
            pass
    finally:
        try:
            await response.write_eof()
        except Exception:  # noqa: BLE001
            pass
    return response


# ── app factory ───────────────────────────────────────────────────────────
def create_app(
    role_factory: Callable[..., Any],
    *,
    token: Optional[str] = None,
    insecure: bool = False,
    name: str = "Assistant",
    registry: Optional[SessionRegistry] = None,
) -> web.Application:
    """Build the AG-UI ``aiohttp`` app over a shared engine ``role_factory``.

    ``role_factory`` is the ``EngineBuild`` closure (from ``cli.app.build_engine``)
    every session is minted from — so a network host and the terminal host share
    one construction path. Auth is REQUIRED: pass a ``token`` (bearer) or set
    ``insecure=True`` to explicitly run ungated (logged loudly). ``registry`` may
    be injected for tests; otherwise a fresh one wraps the factory.
    """
    if token is None and not insecure:
        raise ValueError("AG-UI server requires an auth token; pass token=... or insecure=True to opt out")
    if token is None:
        logger.warning("AG-UI server running WITHOUT auth (insecure=True) — do not expose publicly")

    app = web.Application(middlewares=[_auth_middleware])
    app[_REGISTRY_KEY] = registry if registry is not None else SessionRegistry(role_factory, name=name)
    app[_AUTH_KEY] = token  # None == insecure (middleware skips the gate)
    app[_BROKER_KEY] = PromptBroker()  # app-scoped HITL rendezvous (shared across requests)

    app.router.add_get("/info", _handle_info)
    app.router.add_post("/connect", _handle_connect)
    app.router.add_post("/stop/{tid}", _handle_stop)
    app.router.add_post("/agent/{id}/run", _handle_run)
    app.router.add_post("/respond", _handle_respond)
    # Also accept the agent-scoped form so a client can post either path.
    app.router.add_post("/agent/{id}/respond", _handle_respond)

    async def _on_cleanup(app_: web.Application) -> None:
        app_[_BROKER_KEY].cancel_all("server shutdown")
        await app_[_REGISTRY_KEY].aclose()

    app.on_cleanup.append(_on_cleanup)
    return app


def serve(
    role_factory: Callable[..., Any],
    *,
    host: str = "127.0.0.1",
    port: int = 8808,
    token: Optional[str] = None,
    insecure: bool = False,
    name: str = "Assistant",
) -> None:
    """Blocking entrypoint: build the app and run it (``mote serve --agui``).

    Binds ``127.0.0.1`` by default (a publish-safe floor — never ``0.0.0.0``
    implicitly). ``web.run_app`` owns the event loop + graceful shutdown.
    """
    app = create_app(role_factory, token=token, insecure=insecure, name=name)
    logger.info(f"AG-UI server on http://{host}:{port} (auth={'on' if token else 'OFF'})")
    web.run_app(app, host=host, port=port, print=None)


__all__ = ["create_app", "serve"]
