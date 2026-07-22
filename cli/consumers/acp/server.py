#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""``AcpServer`` — the ACP stdio JSON-RPC agent peer (Phase 4 transport).

The network host for ACP (Agent Client Protocol, https://agentclientprotocol.com)
— the stdio JSON-RPC protocol a Zed-style editor speaks to an agent. Where the
AG-UI host (``consumers/agui/server.py``) owns an aiohttp socket + SSE, this host
owns ONE bidirectional JSON-RPC link over ``stdin``/``stdout`` and drives one turn
per ``session/prompt`` request. Like the AG-UI host it borrows every non-transport
concern from an existing, unit-tested layer:

* **multi-session** — a shared :class:`SessionRegistry` keeps ``sessionId →
  {control, role}`` resident across turns; each ``session/prompt`` pulls its
  session from it.
* **per-turn presentation** — a :class:`ConnectionScope` wraps a fresh
  :class:`AcpConsumer` (its sink = a ``session/update`` notification writer) + an
  :class:`AcpPort` (carries the prompt text + a live ``session/request_permission``
  sender) over that session, subscribes its own projector to the role bus, drives
  one turn, tears the edge down on close.
* **wire shapes** — the pure ``_wire/acp`` mapper (via the consumer + port).

**Why not reuse ``roles/lsp/jsonrpc.py``.** That endpoint is *client-shaped*: it
routes inbound id-bearing messages only as responses to its own pending requests
(there is no path to *reply* to a request the peer sends us), and it frames with
LSP ``Content-Length:`` headers. ACP is the mirror image — mote is the AGENT, so
it RECEIVES id-bearing requests (``initialize`` / ``session/*``) it must answer,
and the wire is newline-delimited JSON (NDJSON), one JSON object per line. So this
module carries a small server-side JSON-RPC loop (:class:`_StdioEndpoint`) that
can dispatch an inbound request to a handler AND write its reply, while still
supporting the *outbound* request half (``session/request_permission``) the port
needs. It shares the same fail-safe posture as ``jsonrpc.py`` (a dead pipe / bad
frame ends the loop, never raises into a turn).

Security (§六 publish floor): stdio has NO network surface — the transport is the
process's own pipes, owned by whoever launched it (the editor). So unlike the
AG-UI host there is no bind address or bearer token to gate; the trust boundary is
the parent process. (An editor that launches mote already trusts it to run tools.)
"""

from __future__ import annotations

import asyncio
import json
import sys
from typing import Any, Awaitable, Callable, Dict, Optional, Set

from mote.cli import backend
from mote.cli.consumers.acp.consumer import AcpConsumer
from mote.cli.consumers.acp.port import DEFAULT_PERMISSION_TIMEOUT_S, AcpPort
from mote.cli.serving import ConnectionScope, SessionRegistry
from mote.common.logs import logger
from mote.roles.lsp.jsonrpc import _rpc_error

# ── ACP protocol constants (verified against the v1 Rust schema) ────────────
ACP_PROTOCOL_VERSION = 1  # integer per the schema (NOT a "1.0" string)

# Agent-side methods (client → agent requests / notifications we handle).
M_INITIALIZE = "initialize"
M_SESSION_NEW = "session/new"
M_SESSION_LOAD = "session/load"
M_SESSION_FORK = "session/fork"
M_SESSION_PROMPT = "session/prompt"
M_SESSION_CANCEL = "session/cancel"  # notification (no id)

# Client-side methods (agent → client). ``session/update`` is the consumer sink;
# ``session/request_permission`` lives in the port (its sole sender).
M_SESSION_UPDATE = "session/update"  # notification

# StopReason values (snake_case wire).
STOP_END_TURN = "end_turn"
STOP_CANCELLED = "cancelled"

# JSON-RPC error codes (subset of the standard table we actually emit).
ERR_METHOD_NOT_FOUND = -32601
ERR_INVALID_PARAMS = -32602
ERR_INTERNAL = -32603

#: An async handler for one inbound request: ``params -> result dict``.
Handler = Callable[[Dict[str, Any]], Awaitable[Dict[str, Any]]]


class JsonRpcError(Exception):
    """A handler-raised error carrying a JSON-RPC ``code`` + ``message``.

    The dispatch loop turns this into a proper ``{error:{code,message}}`` reply
    instead of a bare internal error, so a client sees a well-formed rejection
    (e.g. unknown session id → ``INVALID_PARAMS``).
    """

    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class _StdioEndpoint:
    """A server-side bidirectional JSON-RPC 2.0 peer over NDJSON stdio streams.

    Reads one JSON object per line from *reader*; an inbound **request** (has
    ``id`` + ``method``) is dispatched to the registered async handler and its
    return value written back as a ``result`` reply (a raised
    :class:`JsonRpcError` → an ``error`` reply; any other exception →
    ``INTERNAL``). An inbound **notification** (``method``, no ``id``) is handed
    to the notification sink with no reply. An inbound **response** (``id`` +
    ``result``/``error``) resolves a pending *outbound* request future — the
    half the port uses for ``session/request_permission``.

    Fail-safe throughout (mirrors ``roles/lsp/jsonrpc.py``): a dead pipe or a
    malformed line ends the read loop and fails pending outbound requests rather
    than raising into a turn.
    """

    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: Any,
        *,
        handlers: Dict[str, Handler],
        on_notification: Callable[[str, Dict[str, Any]], None],
    ) -> None:
        self._reader = reader
        self._writer = writer
        self._handlers = handlers
        self._on_notification = on_notification
        self._next_id = 0
        self._pending: Dict[int, asyncio.Future] = {}
        self._write_lock = asyncio.Lock()
        self._closed = False
        # In-flight inbound request handlers. Each request is dispatched as its
        # own task so a long-running one (``session/prompt`` drives a whole turn)
        # does NOT block the read loop — otherwise a ``session/cancel``
        # notification (or a ``session/request_permission`` reply the turn is
        # itself awaiting) could never be read, deadlocking the link.
        self._inflight: Set[asyncio.Task] = set()

    # ------------------------------------------------------------------
    # Outbound request half (the port's session/request_permission sender)
    # ------------------------------------------------------------------
    async def request(self, method: str, params: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Send an id-bearing request to the client, await its reply (or None).

        Returns the reply's ``result`` on success, ``None`` on a closed link or
        an error reply (the caller — the port — treats ``None`` as fail-safe).
        """
        if self._closed:
            return None
        self._next_id += 1
        req_id = self._next_id
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[req_id] = future
        await self._write({"jsonrpc": "2.0", "id": req_id, "method": method, "params": params})
        try:
            return await future
        except Exception as exc:  # noqa: BLE001 — an error reply / teardown → fail-safe None
            logger.debug(f"AcpServer: outbound {method} failed: {exc}")
            return None
        finally:
            self._pending.pop(req_id, None)

    # ------------------------------------------------------------------
    # Read loop + dispatch
    # ------------------------------------------------------------------
    async def serve_forever(self) -> None:
        """Run the read loop until EOF/error (the client closing stdin)."""
        try:
            while True:
                line = await self._reader.readline()
                if not line:  # EOF — client closed the link
                    break
                message = self._decode(line)
                if message is not None:
                    await self._dispatch(message)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — any transport error ends the loop
            logger.debug(f"AcpServer: read loop ended on transport error: {exc}")
        finally:
            self._closed = True
            self._fail_pending()
            # Cancel any handler still running when the link dropped (a turn
            # mid-flight against a dead client) so nothing leaks past the loop.
            for task in list(self._inflight):
                if not task.done():
                    task.cancel()

    @staticmethod
    def _decode(line: bytes) -> Optional[Dict[str, Any]]:
        try:
            obj = json.loads(line.decode("utf-8"))
            return obj if isinstance(obj, dict) else None
        except (ValueError, UnicodeDecodeError):
            return None  # skip a malformed line without killing the loop

    async def _dispatch(self, message: Dict[str, Any]) -> None:
        msg_id = message.get("id")
        method = message.get("method")
        # A reply to one of OUR outbound requests (id + result/error, no method).
        if method is None and msg_id is not None:
            self._resolve(msg_id, message)
            return
        if not isinstance(method, str):
            return
        params = message.get("params")
        if not isinstance(params, dict):
            params = {}
        # A notification (method, no id) — dispatch, never reply.
        if msg_id is None:
            try:
                self._on_notification(method, params)
            except Exception as exc:  # noqa: BLE001 — handler errors never break transport
                logger.debug(f"AcpServer: notification {method!r} raised: {exc}")
            return
        # An inbound request (method + id) — handle it as its OWN task so the
        # read loop keeps servicing the link while the handler runs (a prompt
        # turn may await a permission reply that arrives on this same loop).
        task = asyncio.ensure_future(self._handle_request(msg_id, method, params))
        self._inflight.add(task)
        task.add_done_callback(self._inflight.discard)

    async def _handle_request(self, msg_id: Any, method: str, params: Dict[str, Any]) -> None:
        handler = self._handlers.get(method)
        if handler is None:
            await self._reply_error(msg_id, ERR_METHOD_NOT_FOUND, f"method not found: {method}")
            return
        try:
            result = await handler(params)
            await self._write({"jsonrpc": "2.0", "id": msg_id, "result": result})
        except JsonRpcError as exc:
            await self._reply_error(msg_id, exc.code, exc.message)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — a handler crash → INTERNAL, link survives
            logger.warning(f"AcpServer: handler {method!r} failed: {exc}")
            await self._reply_error(msg_id, ERR_INTERNAL, str(exc))

    def _resolve(self, msg_id: Any, message: Dict[str, Any]) -> None:
        future = self._pending.get(msg_id)
        if future is None or future.done():
            return
        if "error" in message:
            future.set_exception(_rpc_error(message.get("error") or {}))
        else:
            future.set_result(message.get("result"))

    # ------------------------------------------------------------------
    # Wire writes
    # ------------------------------------------------------------------
    async def notify(self, method: str, params: Dict[str, Any]) -> None:
        """Send a notification (no id, no reply) — the ``session/update`` path."""
        if self._closed:
            return
        await self._write({"jsonrpc": "2.0", "method": method, "params": params})

    async def _reply_error(self, msg_id: Any, code: int, message: str) -> None:
        await self._write({"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}})

    async def _write(self, message: Dict[str, Any]) -> None:
        """Serialize *message* as one NDJSON line and write it (lock-serialized)."""
        if self._closed:
            return
        try:
            data = (json.dumps(message, ensure_ascii=False) + "\n").encode("utf-8")
        except (TypeError, ValueError) as exc:
            logger.warning(f"AcpServer: could not serialize outbound message: {exc}")
            return
        async with self._write_lock:
            try:
                self._writer.write(data)
                drain = getattr(self._writer, "drain", None)
                if drain is not None:
                    await drain()
            except Exception as exc:  # noqa: BLE001 — dead pipe; read loop tears down
                logger.debug(f"AcpServer: write to dead pipe failed: {exc}")
                self._closed = True

    def _fail_pending(self) -> None:
        for future in self._pending.values():
            if not future.done():
                future.set_exception(ConnectionError("endpoint closed"))
        self._pending.clear()


class AcpServer:
    """The ACP agent: dispatch ``initialize`` / ``session/*`` over one stdio link.

    Owns a :class:`SessionRegistry` (resident sessions across prompts) and the
    :class:`_StdioEndpoint` (the wire). Each ``session/prompt`` opens a
    :class:`ConnectionScope` binding a fresh :class:`AcpConsumer` (sink =
    ``session/update`` notifications for THAT session) + :class:`AcpPort` (the
    prompt text + an outbound ``session/request_permission`` sender) and drives
    one turn, returning ``{stopReason}``. A ``session/cancel`` notification
    interrupts the in-flight turn so its prompt reply resolves ``cancelled``.
    """

    def __init__(self, registry: SessionRegistry, *, name: str = "Assistant") -> None:
        self._registry = registry
        self._name = name
        self._endpoint: Optional[_StdioEndpoint] = None
        # sessionIds with a live turn, so a cancel can look the session back up in
        # the registry and interrupt it (the session object itself lives there —
        # no need to duplicate control+agent here).
        self._active_turns: Set[str] = set()

    # ------------------------------------------------------------------
    # Wire binding
    # ------------------------------------------------------------------
    def bind(self, reader: asyncio.StreamReader, writer: Any) -> _StdioEndpoint:
        """Bind the stdio streams + build the endpoint (handlers + notifications)."""
        handlers: Dict[str, Handler] = {
            M_INITIALIZE: self._on_initialize,
            M_SESSION_NEW: self._on_session_new,
            M_SESSION_LOAD: self._on_session_load,
            M_SESSION_FORK: self._on_session_fork,
            M_SESSION_PROMPT: self._on_session_prompt,
        }
        self._endpoint = _StdioEndpoint(reader, writer, handlers=handlers, on_notification=self._on_notification)
        return self._endpoint

    async def serve(self, reader: asyncio.StreamReader, writer: Any) -> None:
        """Bind + run the read loop to completion (client closing stdin ends it)."""
        endpoint = self.bind(reader, writer)
        await endpoint.serve_forever()
        await self._registry.aclose()

    # ------------------------------------------------------------------
    # Request handlers
    # ------------------------------------------------------------------
    async def _on_initialize(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Advertise the protocol version + this agent's capabilities.

        We accept whatever ``protocolVersion`` the client offers and answer with
        our own (v1); ``loadSession=True`` (we resume rollouts), all prompt media
        flags off (ACP image/audio content need inline base64 the mapper degrades
        to text — see ``_wire/acp._on_media``).
        """
        return {
            "protocolVersion": ACP_PROTOCOL_VERSION,
            "agentCapabilities": {
                "loadSession": True,
                "promptCapabilities": {
                    "image": False,
                    "audio": False,
                    "embeddedContext": False,
                },
            },
            "authMethods": [],
            "agentInfo": {"name": "mote", "version": "1"},
        }

    async def _on_session_new(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Mint a fresh resident session; return its ``sessionId``.

        A brand-new thread (no id passed to the registry) so a subsequent
        ``session/prompt`` reuses it. ``cwd`` / ``mcpServers`` from the request
        are accepted but not yet threaded (the shared engine's cwd governs) — a
        forward-compatible client tolerates the omission.
        """
        session = await self._registry.get_or_create(None)
        return {"sessionId": session.session_id}

    async def _on_session_load(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Resume a persisted session by ``sessionId`` (resident across restarts).

        ``get_or_create`` resumes the rollout when the id names one; an unknown
        id starts a fresh thread under that id. Returns an empty envelope (no
        modes/configOptions advertised).
        """
        session_id = self._session_id(params)
        await self._registry.get_or_create(session_id)
        return {}

    async def _on_session_fork(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Branch a sibling session off ``sessionId`` at its current history.

        Ensures the source is resident, forks its role (independent afterwards),
        wires + starts a control plane for the fork via the registry's build
        path, and returns the fork's ``sessionId``. Falls back to a plain new
        session if the engine can't fork (best-effort, matching ``fork_role``).
        """
        session_id = self._session_id(params)
        source = await self._registry.get_or_create(session_id)
        forked = backend.fork_role(source.role)
        if forked is None:
            # Engine can't fork this role — degrade to a fresh session so the
            # client still gets a usable id rather than an error.
            fresh = await self._registry.get_or_create(None)
            return {"sessionId": fresh.session_id}
        resident = self._registry.adopt(forked)
        return {"sessionId": resident.session_id}

    async def _on_session_prompt(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Drive ONE turn for ``sessionId``; return ``{stopReason}``.

        Opens a :class:`ConnectionScope` binding a fresh :class:`AcpConsumer`
        (sink = ``session/update`` for this session) + :class:`AcpPort` (the
        prompt text + an outbound ``session/request_permission`` sender bound to
        this session). Registers the turn as active so a ``session/cancel`` can
        interrupt it, drives it to quiescence, then returns ``end_turn`` (or
        ``cancelled`` if it was interrupted).
        """
        session_id = self._session_id(params)
        text = self._prompt_text(params)
        session = await self._registry.get_or_create(session_id)
        endpoint = self._endpoint

        async def sink(update: Dict[str, Any]) -> None:
            # Wrap each mapper ``update`` in the ACP ``{sessionId, update}``
            # envelope + send as a ``session/update`` notification.
            if endpoint is not None:
                await endpoint.notify(M_SESSION_UPDATE, {"sessionId": session.session_id, "update": update})

        async def request_permission(method: str, perm_params: Dict[str, Any]) -> Optional[Dict[str, Any]]:
            if endpoint is None:
                return None
            return await endpoint.request(method, perm_params)

        consumer = AcpConsumer(session_id=session.session_id, sink=sink)
        port = AcpPort(
            text,
            session_id=session.session_id,
            request=request_permission,
            timeout_s=DEFAULT_PERMISSION_TIMEOUT_S,
        )
        scope = ConnectionScope(session, consumers=[consumer], port=port)

        self._active_turns.add(session.session_id)
        cancelled = False
        try:
            async with scope:
                message = backend.turn_message(text)
                await scope.run_turn(message)
        except asyncio.CancelledError:
            cancelled = True
            logger.info(f"AcpServer: prompt for {session.session_id[:8]} cancelled")
        except Exception as exc:  # noqa: BLE001 — a turn failure still returns a stopReason
            logger.warning(f"AcpServer: prompt for {session.session_id[:8]} failed: {exc}")
        finally:
            self._active_turns.discard(session.session_id)
        return {"stopReason": STOP_CANCELLED if cancelled else STOP_END_TURN}

    # ------------------------------------------------------------------
    # Notification handler
    # ------------------------------------------------------------------
    def _on_notification(self, method: str, params: Dict[str, Any]) -> None:
        """Handle a client notification. Only ``session/cancel`` is actionable."""
        if method != M_SESSION_CANCEL:
            return
        session_id = params.get("sessionId")
        if not isinstance(session_id, str) or session_id not in self._active_turns:
            return
        session = self._registry.get(session_id)
        if session is None:
            return
        # Interrupt the in-flight turn; its prompt reply then resolves cancelled.
        # Best-effort, scheduled on the loop (the notification path is sync).
        try:
            asyncio.ensure_future(session.control.interrupt(session.agent_id))
        except Exception as exc:  # noqa: BLE001 — cancel is best-effort
            logger.debug(f"AcpServer: cancel for {session_id[:8]} failed: {exc}")

    # ------------------------------------------------------------------
    # Param helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _session_id(params: Dict[str, Any]) -> str:
        session_id = params.get("sessionId")
        if not isinstance(session_id, str) or not session_id:
            raise JsonRpcError(ERR_INVALID_PARAMS, "sessionId required")
        return session_id

    @staticmethod
    def _prompt_text(params: Dict[str, Any]) -> str:
        """Extract the turn's text from a ``prompt: [ContentBlock]`` array.

        ACP posts ``{sessionId, prompt:[ContentBlock,...]}``; each Text block is
        ``{type:"text", text}``. We concatenate every text block's text (the one
        block-type every agent MUST support); non-text blocks (image/resource)
        are skipped here — the mapper degrades media on the OUTPUT side, and this
        agent advertises no prompt media capability.
        """
        prompt = params.get("prompt")
        if not isinstance(prompt, list):
            return ""
        parts = []
        for block in prompt:
            if isinstance(block, dict) and block.get("type") == "text":
                text = block.get("text")
                if isinstance(text, str):
                    parts.append(text)
        return "".join(parts)


def serve(
    role_factory: Callable[..., Any],
    *,
    name: str = "Assistant",
    registry: Optional[SessionRegistry] = None,
) -> None:
    """Blocking entrypoint: bind the ACP agent to stdio + run (``mote --serve acp``).

    Builds (or adopts) a :class:`SessionRegistry` over the shared engine
    ``role_factory`` — the same construction path the terminal + AG-UI hosts use
    — then runs the JSON-RPC read loop over ``stdin``/``stdout`` until the client
    closes the link. Owns the event loop for its lifetime.
    """
    reg = registry if registry is not None else SessionRegistry(role_factory, name=name)
    server = AcpServer(reg, name=name)
    logger.info("ACP server on stdio (JSON-RPC over stdin/stdout)")
    asyncio.run(_run_stdio(server))


async def _run_stdio(server: AcpServer) -> None:
    """Wire asyncio stream readers/writers onto the process stdio + serve."""
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader()
    protocol = asyncio.StreamReaderProtocol(reader)
    await loop.connect_read_pipe(lambda: protocol, sys.stdin)
    w_transport, w_protocol = await loop.connect_write_pipe(asyncio.streams.FlowControlMixin, sys.stdout)
    writer = asyncio.StreamWriter(w_transport, w_protocol, reader, loop)
    await server.serve(reader, writer)


__all__ = ["AcpServer", "JsonRpcError", "serve", "ACP_PROTOCOL_VERSION"]
