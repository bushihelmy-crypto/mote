import json
from contextlib import asynccontextmanager
from dataclasses import asdict, is_dataclass
from datetime import timedelta
from typing import Any, AsyncGenerator
from urllib.parse import urljoin, urlparse

import anyio
import httpx
import mcp.types as types
from anyio.abc import TaskStatus
from anyio.streams.memory import MemoryObjectReceiveStream, MemoryObjectSendStream
from httpx_sse import aconnect_sse
from mcp.shared.message import SessionMessage
from pydantic_core import to_jsonable_python

from mote.common.logs import logger
from mote.common.utils.pydantic_compat import model_dump, model_dump_json, model_validate_json
from mote.executor.mcp.client.base import EnhancedClientSession, MCPBaseClient


def _serialize_message(message: Any) -> Any:
    # Try each serialization path in order of preference; a failure just falls
    # through to the next candidate (the broad excepts are deliberate — any
    # serializer that raises is simply not applicable to this message shape).
    if hasattr(message, "message") and isinstance(getattr(message, "message", None), types.JSONRPCMessage):
        try:
            return json.loads(model_dump_json(message.message, by_alias=True, exclude_none=True))
        except Exception:
            pass
    if isinstance(message, types.JSONRPCMessage):
        try:
            return json.loads(model_dump_json(message, by_alias=True, exclude_none=True))
        except Exception:
            pass
    try:
        return to_jsonable_python(message, by_alias=True, exclude_none=True)
    except Exception:
        pass
    if hasattr(message, "model_dump_json"):
        try:
            return json.loads(model_dump_json(message, by_alias=True, exclude_none=True))
        except Exception:
            pass
    if hasattr(message, "model_dump"):
        return model_dump(message, by_alias=True, mode="json", exclude_none=True)
    if hasattr(message, "json"):
        try:
            return json.loads(message.json())
        except Exception:
            pass
    if hasattr(message, "dict"):
        try:
            return message.dict(by_alias=True, exclude_none=True)
        except TypeError:
            return message.dict()
    if is_dataclass(message) and not isinstance(message, type):
        return asdict(message)
    return message


@asynccontextmanager
async def enhanced_sse_client(
    url: str,
    headers: dict[str, Any] | None = None,
    timeout: float = 5,
    sse_read_timeout: float = 60 * 5,
):
    """
    Enhanced SSE client copied from `mcp.client.sse.sse_client`.
    Fixed hanging issues when encountering errors like 'peer closed connection without sending complete message body'
    or 'Name or service not known' by raising exceptions for retry instead of hanging forever.
    """
    read_stream: MemoryObjectReceiveStream[SessionMessage | Exception]
    read_stream_writer: MemoryObjectSendStream[SessionMessage | Exception]

    write_stream: MemoryObjectSendStream[SessionMessage]
    write_stream_reader: MemoryObjectReceiveStream[SessionMessage]

    read_stream_writer, read_stream = anyio.create_memory_object_stream(0)
    write_stream, write_stream_reader = anyio.create_memory_object_stream(0)

    async with anyio.create_task_group() as tg:
        try:
            async with httpx.AsyncClient(headers=headers) as client:
                async with aconnect_sse(
                    client,
                    "GET",
                    url,
                    timeout=httpx.Timeout(timeout, read=sse_read_timeout),
                ) as event_source:
                    event_source.response.raise_for_status()

                    async def sse_reader(
                        task_status: TaskStatus[str] = anyio.TASK_STATUS_IGNORED,
                    ):
                        try:
                            async for sse in event_source.aiter_sse():
                                match sse.event:
                                    case "endpoint":
                                        endpoint_url = urljoin(url, sse.data)

                                        url_parsed = urlparse(url)
                                        endpoint_parsed = urlparse(endpoint_url)
                                        if (
                                            url_parsed.netloc != endpoint_parsed.netloc
                                            or url_parsed.scheme != endpoint_parsed.scheme
                                        ):
                                            error_msg = (
                                                "Endpoint origin does not match " f"connection origin: {endpoint_url}"
                                            )
                                            raise ValueError(error_msg)

                                        task_status.started(endpoint_url)

                                    case "message":
                                        try:
                                            message = model_validate_json(types.JSONRPCMessage, sse.data)  # noqa: E501
                                        except Exception as exc:
                                            logger.error(f"Error parsing server message: {exc}")
                                            await read_stream_writer.send(exc)
                                            continue

                                        session_message = SessionMessage(message)
                                        await read_stream_writer.send(session_message)
                                    case _:
                                        logger.warning(f"Unknown SSE event: {sse.event}")
                        except Exception as exc:
                            await read_stream_writer.send(exc)
                            raise exc  # Enhanced: handle the error, such as 'peer closed connection without sending complete message body (incomplete chunked read)', raise it and retry
                        finally:
                            await read_stream_writer.aclose()

                    async def post_writer(endpoint_url: str):
                        try:
                            async with write_stream_reader:
                                async for message in write_stream_reader:
                                    response = await client.post(
                                        endpoint_url,
                                        json=_serialize_message(message),
                                    )
                                    response.raise_for_status()
                        except Exception as exc:
                            raise exc  # Enhanced: handle the error, such as 'Name or service not known', raise it and retry
                        finally:
                            await write_stream.aclose()

                    endpoint_url = await tg.start(sse_reader)
                    tg.start_soon(post_writer, endpoint_url)

                    try:
                        yield read_stream, write_stream
                    finally:
                        tg.cancel_scope.cancel()
        finally:
            await read_stream_writer.aclose()
            await write_stream.aclose()


class MCPSSEClient(MCPBaseClient):
    @asynccontextmanager
    async def get_session(self) -> AsyncGenerator[EnhancedClientSession, None]:
        """Create new session to fix: 'RuntimeError: Attempted to exit cancel scope in a different task than it was entered in'"""
        url = self.server_config.url
        assert url is not None, "SSE server config requires a url"
        async with enhanced_sse_client(url, sse_read_timeout=self.server_config.sse_read_timeout or 60 * 5) as streams:
            read_timeout_seconds = timedelta(seconds=self.server_config.tool_call_timeout or 60)
            async with EnhancedClientSession(*streams, read_timeout_seconds=read_timeout_seconds) as session:
                await session.initialize()
                yield session
