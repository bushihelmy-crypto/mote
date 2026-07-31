"""Retry-free OpenAI-compatible Realtime WebSocket transport."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode, urlsplit, urlunsplit

from aiohttp import ClientWSTimeout, WSMsgType
from aiohttp.client_ws import ClientWebSocketResponse

from mote.contracts.inference.executions import BoundExecutionRequest, SessionApplicationMessage
from mote.contracts.inference.transport import ProviderWireResult
from mote.contracts.ports.inference.provider_transport import WireLifecycleSink
from mote.product.models.transports.connections.aiohttp import AioHttpConnectionLease

AuthHeaders = Callable[[], Awaitable[Mapping[str, str]]]


@dataclass(frozen=True, slots=True)
class OpenAIRealtimeOpenResult:
    connection: "OpenAIRealtimeConnection"
    wire_result: ProviderWireResult


class OpenAIRealtimeTransport:
    provider = "openai"
    wire_protocol = "openai_realtime_websocket"

    def __init__(
        self,
        *,
        endpoint_id: str,
        base_url: str,
        connection: AioHttpConnectionLease,
        auth_headers: AuthHeaders,
        max_frame_bytes: int = 4 * 1024 * 1024,
    ) -> None:
        if not endpoint_id or max_frame_bytes <= 0:
            raise ValueError("invalid Realtime transport configuration")
        self.endpoint_id = endpoint_id
        self._base_url = base_url
        self._connection = connection
        self._auth_headers = auth_headers
        self._max_frame_bytes = max_frame_bytes
        self._closed = False
        self._sessions: set[OpenAIRealtimeConnection] = set()

    async def open_once(
        self,
        request: BoundExecutionRequest,
        *,
        local_deadline: float,
        lifecycle: WireLifecycleSink,
    ) -> OpenAIRealtimeOpenResult:
        if self._closed:
            raise RuntimeError("Realtime transport is closed")
        remaining = local_deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            raise TimeoutError("Realtime deadline exceeded before handshake")
        model = request.payload.get("model")
        if not isinstance(model, str) or not model:
            raise ValueError("Realtime open request requires model")
        headers = _validated_headers(await self._auth_headers())
        await lifecycle.wire_started()
        socket = await self._connection.session.ws_connect(
            _realtime_url(self._base_url, model),
            headers=headers,
            timeout=ClientWSTimeout(ws_close=remaining),
            autoclose=False,
            autoping=True,
            compress=0,
            max_msg_size=self._max_frame_bytes,
        )
        await lifecycle.response_started()
        realtime = OpenAIRealtimeConnection(
            socket,
            max_frame_bytes=self._max_frame_bytes,
            on_close=self._sessions.discard,
        )
        self._sessions.add(realtime)
        return OpenAIRealtimeOpenResult(
            connection=realtime,
            wire_result=ProviderWireResult(payload={"connected": True}),
        )

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        await asyncio.gather(*(session.close("transport closing") for session in tuple(self._sessions)))
        await self._connection.release()


class OpenAIRealtimeConnection:
    def __init__(
        self,
        socket: ClientWebSocketResponse,
        *,
        max_frame_bytes: int,
        on_close: Callable[["OpenAIRealtimeConnection"], None],
    ) -> None:
        self._socket = socket
        self._max_frame_bytes = max_frame_bytes
        self._on_close = on_close
        self._send_lock = asyncio.Lock()
        self._closed = False

    async def send_once(
        self,
        message: SessionApplicationMessage,
        *,
        local_deadline: float,
        lifecycle: WireLifecycleSink,
    ) -> ProviderWireResult:
        if self._closed:
            raise RuntimeError("Realtime connection is closed")
        payload = {"type": message.message_type, **message.payload}
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode()
        if len(encoded) > self._max_frame_bytes:
            raise ValueError("Realtime outbound frame exceeds configured limit")
        remaining = local_deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            raise TimeoutError("Realtime message deadline exceeded")
        async with self._send_lock:
            await lifecycle.wire_started()
            await asyncio.wait_for(self._socket.send_bytes(encoded), timeout=remaining)
            await lifecycle.response_started()
        return ProviderWireResult(payload={"sent": True, "bytes": len(encoded)})

    async def inbound(self) -> AsyncIterator[dict[str, Any]]:
        async for message in self._socket:
            if message.type in {WSMsgType.TEXT, WSMsgType.BINARY}:
                raw = message.data.encode() if isinstance(message.data, str) else message.data
                if len(raw) > self._max_frame_bytes:
                    raise ValueError("Realtime inbound frame exceeds configured limit")
                try:
                    payload = json.loads(raw)
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise ValueError("Realtime provider returned malformed JSON") from exc
                if not isinstance(payload, dict):
                    raise ValueError("Realtime event must be a JSON object")
                if payload.get("type") == "error" or "error" in payload:
                    raise RuntimeError("Realtime provider returned an error event")
                yield payload
            elif message.type is WSMsgType.ERROR:
                raise ConnectionError("Realtime WebSocket failed")
            elif message.type in {WSMsgType.CLOSE, WSMsgType.CLOSED, WSMsgType.CLOSING}:
                return

    async def close(self, reason: str) -> None:
        if self._closed:
            return
        self._closed = True
        encoded = reason.encode()[:123].decode(errors="ignore")
        await self._socket.close(code=1000, message=encoded.encode())
        self._on_close(self)


def _realtime_url(base_url: str, model: str) -> str:
    split = urlsplit(base_url)
    if split.scheme != "https" or not split.netloc or split.username or split.password:
        raise ValueError("Realtime base URL must be credential-free HTTPS")
    path = split.path.rstrip("/")
    if not path.endswith("/v1"):
        path += "/v1"
    path += "/realtime"
    return urlunsplit(("wss", split.netloc, path, urlencode({"model": model}), ""))


def _validated_headers(headers: Mapping[str, str]) -> dict[str, str]:
    forbidden = {"connection", "proxy-authorization", "transfer-encoding", "upgrade"}
    normalized = {str(key): str(value) for key, value in headers.items()}
    lowered = {key.lower() for key in normalized}
    if lowered.intersection(forbidden):
        raise ValueError("credential binding returned forbidden WebSocket headers")
    if "authorization" not in lowered:
        raise ValueError("credential binding did not provide authorization")
    return normalized
