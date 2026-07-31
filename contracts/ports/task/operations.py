"""Narrow task lifecycle and result-storage ports."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Protocol

from mote.contracts.ports.conversation.message_activity import MessageActivity
from mote.contracts.ports.conversation.message_sink import MessageSink
from mote.contracts.task.models import SessionId, TaskId, TaskResultRecord


class BackgroundMessageSink(MessageSink, MessageActivity, Protocol):
    pass


class BackgroundTaskService(Protocol):
    def has_pending(self) -> bool:
        ...

    @property
    def pending_count(self) -> int:
        ...

    async def wait_any(self, timeout: float = ...) -> Any:
        ...

    async def wait_for_completion(self, timeout: float | None = ...) -> bool:
        ...

    def set_wake(self, wake: Callable[[], None] | None) -> None:
        ...

    async def aclose(self) -> None:
        ...


class TaskResultRegistry(Protocol):
    def register_task_result(self, task_id: TaskId, content: str) -> None:
        ...

    def unload(self, task_id: TaskId) -> TaskResultRecord | None:
        ...


class TaskOutputLocationPort(Protocol):
    def output_directory(self, session_id: SessionId) -> Path:
        ...


class AgentWakePort(Protocol):
    def wake(self) -> None:
        ...


@dataclass(frozen=True, slots=True)
class BackgroundTaskBuildContext:
    message_sink: BackgroundMessageSink
    wake: AgentWakePort
    output_locations: TaskOutputLocationPort
    session_id: SessionId
    result_registry: TaskResultRegistry


BackgroundTaskServiceFactory = Callable[[BackgroundTaskBuildContext], BackgroundTaskService]


__all__ = [
    "AgentWakePort",
    "BackgroundMessageSink",
    "BackgroundTaskBuildContext",
    "BackgroundTaskService",
    "BackgroundTaskServiceFactory",
    "TaskOutputLocationPort",
    "TaskResultRegistry",
]
