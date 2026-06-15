import asyncio
import os
import typing
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Literal, Optional
from urllib.parse import unquote, urlparse, urlunparse
from uuid import UUID, uuid4

from aiohttp import ClientSession, UnixConnector
from pydantic import BaseModel, Field, PrivateAttr

from metagpt.common.const import METAGPT_REPORTER_DEFAULT_URL

if typing.TYPE_CHECKING:
    from metagpt.roles.role import Role


class _StreamQueueSubscriber:
    """Bus subscriber that forwards streamed LLM tokens into an ``asyncio.Queue``.

    Registered on the active event bus while a reporter's async streaming context
    is open; ``None`` is the sentinel the reporter pushes to end the drain task.
    """

    priority: int = 60

    def __init__(self, queue: asyncio.Queue):
        self._queue = queue

    def handle_sync(self, event) -> None:
        from metagpt.common.events import LLMStreamDeltaEvent

        if isinstance(event, LLMStreamDeltaEvent):
            self._queue.put_nowait(event.token)

    async def handle(self, event):  # observation-only; never influences the fold
        return None

try:
    import requests_unixsocket as requests
except ImportError:
    import requests

from contextvars import ContextVar

CURRENT_ROLE: ContextVar["Role"] = ContextVar("role")


class BlockType(str, Enum):
    """Enumeration for different types of blocks."""

    TERMINAL = "Terminal"
    TASK = "Task"
    BROWSER = "Browser"
    BROWSER_RT = "Browser-RT"
    EDITOR = "Editor"
    GALLERY = "Gallery"
    NOTEBOOK = "Notebook"
    DOCS = "Docs"
    THOUGHT = "Thought"
    ACTION = "Action"
    WEBSEARCH = "WebSearch"
    RECOMMEND = "Recommend"
    ARTIFACTS = "Artifacts"
    ACTION_DATA = "ActionData"


END_MARKER_NAME = "end_marker"


class ResourceReporter(BaseModel):
    """Base class for resource reporting."""

    block: BlockType = Field(description="The type of block that is reporting the resource")
    uuid: UUID = Field(default_factory=uuid4, description="The unique identifier for the resource")
    enable_llm_stream: bool = Field(False, description="Indicates whether to connect to an LLM stream for reporting")
    callback_url: str = Field(METAGPT_REPORTER_DEFAULT_URL, description="The URL to which the report should be sent")
    _llm_task: Optional[asyncio.Task] = PrivateAttr(None)
    _llm_queue: Optional[asyncio.Queue] = PrivateAttr(None)
    _llm_sub: Optional["_StreamQueueSubscriber"] = PrivateAttr(None)
    _llm_bus: Any = PrivateAttr(None)

    def report(self, value: Any, name: str, extra: Optional[dict] = None):
        """Synchronously report resource observation data.

        Args:
            value: The data to report.
            name: The type name of the data.
        """
        return self._report(value, name, extra)

    async def async_report(self, value: Any, name: str, extra: Optional[dict] = None):
        """Asynchronously report resource observation data.

        Args:
            value: The data to report.
            name: The type name of the data.
        """
        return await self._async_report(value, name, extra)

    @classmethod
    def set_report_fn(cls, fn: Callable):
        """Set the synchronous report function.

        Args:
            fn: A callable function used for synchronous reporting. For example:

                >>> def _report(self, value: Any, name: str):
                ...     print(value, name)

        """
        cls._report = fn

    @classmethod
    def set_async_report_fn(cls, fn: Callable):
        """Set the asynchronous report function.

        Args:
            fn: A callable function used for asynchronous reporting. For example:

                ```python
                >>> async def _report(self, value: Any, name: str):
                ...     print(value, name)
                ```
        """
        cls._async_report = fn

    def _report(self, value: Any, name: str, extra: Optional[dict] = None):
        if not self.callback_url:
            return

        data = self._format_data(value, name, extra)
        resp = requests.post(self.callback_url, json=data)
        resp.raise_for_status()
        return resp.text

    async def _async_report(self, value: Any, name: str, extra: Optional[dict] = None):
        if not self.callback_url:
            return

        data = self._format_data(value, name, extra)
        url = self.callback_url
        _result = urlparse(url)
        session_kwargs = {}
        if _result.scheme.endswith("+unix"):
            parsed_list = list(_result)
            parsed_list[0] = parsed_list[0][:-5]
            parsed_list[1] = "fake.org"
            url = urlunparse(parsed_list)
            session_kwargs["connector"] = UnixConnector(path=unquote(_result.netloc))

        async with ClientSession(**session_kwargs) as client:
            async with client.post(url, json=data) as resp:
                resp.raise_for_status()
                return await resp.text()

    def _format_data(self, value, name, extra):
        data = self.model_dump(mode="json", exclude=("callback_url", "llm_stream"))
        if isinstance(value, BaseModel):
            value = value.model_dump(mode="json")
        elif isinstance(value, Path):
            value = str(value)

        if name == "path":
            value = os.path.abspath(value)
        data["value"] = value
        data["name"] = name
        role = CURRENT_ROLE.get(None)
        if role:
            role_name = role.name
        else:
            role_name = os.environ.get("METAGPT_ROLE")
        data["role"] = role_name
        if extra:
            data["extra"] = extra
        return data

    def __enter__(self):
        """Enter the synchronous streaming callback context."""
        return self

    def __exit__(self, *args, **kwargs):
        """Exit the synchronous streaming callback context."""
        self.report(None, END_MARKER_NAME)

    async def __aenter__(self):
        """Enter the asynchronous streaming callback context.

        Subscribes a :class:`_StreamQueueSubscriber` to the active event bus so
        streamed LLM tokens are drained to this reporter. No-ops when no bus is
        bound (standalone use): nothing to mirror.
        """
        if self.enable_llm_stream:
            from metagpt.common.events import current_bus

            bus = current_bus()
            if bus is not None:
                self._llm_queue = asyncio.Queue()
                self._llm_sub = _StreamQueueSubscriber(self._llm_queue)
                self._llm_bus = bus
                bus.subscribe(self._llm_sub)
                self._llm_task = asyncio.create_task(self._llm_stream_report(self._llm_queue))
        return self

    async def __aexit__(self, exc_type, exc_value, exc_tb):
        """Exit the asynchronous streaming callback context."""
        if self._llm_task is not None and exc_type != asyncio.CancelledError:
            await self._llm_queue.put(None)
            await self._llm_task
        if self._llm_bus is not None and self._llm_sub is not None:
            self._llm_bus.unsubscribe(self._llm_sub)
        self._llm_task = None
        self._llm_queue = None
        self._llm_sub = None
        self._llm_bus = None
        await self.async_report(None, END_MARKER_NAME)

    async def _llm_stream_report(self, queue: asyncio.Queue):
        while True:
            data = await queue.get()
            if data is None:
                return
            await self.async_report(data, "content")

    async def wait_llm_stream_report(self):
        """Wait for the LLM stream report to complete."""
        queue = self._llm_queue
        while self._llm_task and queue is not None:
            if queue.empty():
                break
            await asyncio.sleep(0.01)


class ObjectReporter(ResourceReporter):
    """Callback for reporting complete object resources."""

    def report(self, value: dict, name: Literal["object"] = "object"):
        """Report object resource synchronously."""
        return super().report(value, name)

    async def async_report(self, value: dict, name: Literal["object"] = "object"):
        """Report object resource asynchronously."""
        return await super().async_report(value, name)


class ThoughtReporter(ObjectReporter):
    """Reporter for object resources to Thought Block."""

    block: Literal[BlockType.THOUGHT] = BlockType.THOUGHT


class RecommendReporter(ObjectReporter):
    """Reporter for recommendation items."""

    block: Literal[BlockType.RECOMMEND] = BlockType.RECOMMEND


class ArtifactsReporter(ObjectReporter):
    """Reporter for object resources to Artifacts Block."""

    block: Literal[BlockType.ARTIFACTS] = BlockType.ARTIFACTS
