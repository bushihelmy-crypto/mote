"""JSON-RPC 2.0 over LSP's Content-Length-framed stdio.

A thin transport the LSP server instance drives: it owns one asyncio subprocess's
stdin/stdout and speaks the base protocol every language server uses —
``Content-Length: N\\r\\n\\r\\n<N bytes of JSON>`` frames.

Responsibilities, and nothing more:

- frame/serialize outgoing requests + notifications (``write_*``);
- run a background reader that parses incoming frames and either resolves the
  matching request future (by id) or dispatches a notification to a callback;
- correlate request ids to futures so ``request()`` can await a reply.

This is deliberately ignorant of LSP semantics (initialize / didOpen /
publishDiagnostics) — that lives in ``server.py``. It only knows the wire format.
Everything is best-effort and shutdown-safe: a dead pipe or malformed frame ends
the read loop and fails pending requests rather than raising into callers.
"""

from __future__ import annotations

import asyncio
import json
from typing import Callable, Optional

# Notification dispatch: (method, params) -> None. Sync; the endpoint never
# awaits it (handlers stage work, they don't block the read loop).
NotificationHandler = Callable[[str, dict], None]

_HEADER_SEP = b"\r\n\r\n"
_CONTENT_LENGTH = b"Content-Length:"


class JsonRpcEndpoint:
    """One JSON-RPC endpoint bound to a subprocess's stdio streams.

    Construct with the process's ``stdin`` (a ``StreamWriter``) and ``stdout``
    (a ``StreamReader``); call :meth:`start` to launch the reader, then use
    :meth:`request` / :meth:`notify`. Call :meth:`close` to stop the reader and
    fail any in-flight requests.
    """

    def __init__(
        self,
        writer: asyncio.StreamWriter,
        reader: asyncio.StreamReader,
        on_notification: Optional[NotificationHandler] = None,
    ) -> None:
        self._writer = writer
        self._reader = reader
        self._on_notification = on_notification
        self._next_id = 0
        self._pending: dict[int, asyncio.Future] = {}
        self._read_task: Optional[asyncio.Task] = None
        self._closed = False

    def start(self) -> None:
        """Launch the background read loop (idempotent)."""
        if self._read_task is None:
            self._read_task = asyncio.ensure_future(self._read_loop())

    async def request(self, method: str, params: dict, *, timeout: float) -> dict:
        """Send a request and await its result. Raises on timeout/closed/error."""
        if self._closed:
            raise ConnectionError("JSON-RPC endpoint is closed")
        self._next_id += 1
        req_id = self._next_id
        future: asyncio.Future = asyncio.get_event_loop().create_future()
        self._pending[req_id] = future
        self._write_message(
            {"jsonrpc": "2.0", "id": req_id, "method": method, "params": params}
        )
        try:
            return await asyncio.wait_for(future, timeout=timeout)
        finally:
            self._pending.pop(req_id, None)

    def notify(self, method: str, params: dict) -> None:
        """Send a notification (no id, no reply)."""
        if self._closed:
            return
        self._write_message({"jsonrpc": "2.0", "method": method, "params": params})

    async def close(self) -> None:
        """Stop the reader and fail any pending requests. Idempotent."""
        if self._closed:
            return
        self._closed = True
        if self._read_task is not None:
            self._read_task.cancel()
            try:
                await self._read_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._read_task = None
        self._fail_pending(ConnectionError("endpoint closed"))
        try:
            self._writer.close()
        except Exception:  # noqa: BLE001
            pass

    # --- internals ---------------------------------------------------------

    def _write_message(self, message: dict) -> None:
        """Serialize *message* as a Content-Length frame and write it."""
        body = json.dumps(message).encode("utf-8")
        header = f"Content-Length: {len(body)}\r\n\r\n".encode("ascii")
        try:
            self._writer.write(header + body)
        except Exception:  # noqa: BLE001 — dead pipe; reader loop will tear down
            pass

    async def _read_loop(self) -> None:
        """Parse frames until EOF/error, dispatching responses + notifications."""
        try:
            while True:
                message = await self._read_message()
                if message is None:
                    break
                self._dispatch(message)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — any transport error ends the loop
            pass
        finally:
            self._fail_pending(ConnectionError("reader stopped"))

    async def _read_message(self) -> Optional[dict]:
        """Read one Content-Length frame; return the decoded dict or None at EOF."""
        header = await self._reader.readuntil(_HEADER_SEP)
        length = _parse_content_length(header)
        if length is None:
            return None
        body = await self._reader.readexactly(length)
        try:
            return json.loads(body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return {}  # skip a malformed frame without killing the loop

    def _dispatch(self, message: dict) -> None:
        """Route a decoded message to a pending future or the notification sink."""
        if not isinstance(message, dict):
            return
        msg_id = message.get("id")
        if msg_id is not None and ("result" in message or "error" in message):
            future = self._pending.get(msg_id)
            if future is not None and not future.done():
                if "error" in message:
                    future.set_exception(_rpc_error(message["error"]))
                else:
                    future.set_result(message.get("result"))
            return
        # A request *from* the server (has method + id) or a notification (no id).
        method = message.get("method")
        if method and self._on_notification is not None:
            try:
                self._on_notification(method, message.get("params") or {})
            except Exception:  # noqa: BLE001 — handler errors never break transport
                pass

    def _fail_pending(self, exc: Exception) -> None:
        for future in self._pending.values():
            if not future.done():
                future.set_exception(exc)
        self._pending.clear()


def _parse_content_length(header: bytes) -> Optional[int]:
    """Extract the byte length from a frame header block."""
    for line in header.split(b"\r\n"):
        if line.startswith(_CONTENT_LENGTH):
            try:
                return int(line[len(_CONTENT_LENGTH):].strip())
            except ValueError:
                return None
    return None


def _rpc_error(error: dict) -> Exception:
    """Turn a JSON-RPC error object into an exception."""
    code = error.get("code", "?") if isinstance(error, dict) else "?"
    msg = error.get("message", "") if isinstance(error, dict) else str(error)
    return RuntimeError(f"JSON-RPC error {code}: {msg}")


__all__ = ["JsonRpcEndpoint", "NotificationHandler"]
