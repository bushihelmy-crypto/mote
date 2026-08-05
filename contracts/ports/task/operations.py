"""Narrow task lifecycle and result-storage ports."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol

from mote.contracts.async_work.command import LocalCancelReceipt
from mote.contracts.ports.async_work.local import LocalAsyncWorkCommandPort, LocalAsyncWorkObservationPort
from mote.contracts.ports.conversation.message_activity import MessageActivity
from mote.contracts.ports.conversation.message_sink import MessageSink
from mote.contracts.session.identity import SessionId
from mote.contracts.task.lifecycle import BackgroundTaskDrainReceipt, BackgroundTaskOwner, BackgroundTaskPinSnapshot
from mote.contracts.task.models import TaskId, TaskResultRecord


class BackgroundTaskSnapshot(Protocol):
    task_id: TaskId
    status: str
    command_name: str


class BackgroundMessageSink(MessageSink, MessageActivity, Protocol):
    pass


class BackgroundTaskService(Protocol):
    @property
    def session_id(self) -> SessionId: ...

    def async_work_adapter(self) -> "LocalAsyncWorkAdapter": ...

    def has_pending(self) -> bool: ...

    @property
    def owner(self) -> BackgroundTaskOwner: ...

    def pin_snapshot(self, *, owner: BackgroundTaskOwner) -> BackgroundTaskPinSnapshot: ...

    def close_admission(self, *, owner: BackgroundTaskOwner) -> BackgroundTaskPinSnapshot: ...

    async def drain(
        self,
        *,
        owner: BackgroundTaskOwner,
        timeout_seconds: float,
    ) -> BackgroundTaskDrainReceipt: ...

    def get_task_info(self, task_id: TaskId) -> BackgroundTaskSnapshot | None: ...

    def cancel_current(self, task_id: TaskId, reason: str) -> LocalCancelReceipt: ...

    def mark_retrieved(self, task_id: TaskId) -> None: ...

    @property
    def pending_count(self) -> int: ...

    async def wait_for_completion(self, timeout: float | None = ...) -> bool: ...

    def set_wake(self, wake: Callable[[], None] | None) -> None: ...

    async def aclose(self) -> None: ...


class LocalAsyncWorkAdapter(LocalAsyncWorkCommandPort, LocalAsyncWorkObservationPort, Protocol):
    pass


class TaskResultRegistry(Protocol):
    def register_task_result(self, task_id: TaskId, content: str) -> None: ...

    def unload(self, task_id: TaskId) -> TaskResultRecord | None: ...


class TaskOutputLocationPort(Protocol):
    def output_directory(self, session_id: SessionId) -> Path: ...

    def tool_result_path(self, session_id: SessionId, result_id: str) -> Path: ...


class AgentWakePort(Protocol):
    def wake(self) -> None: ...


@dataclass(frozen=True, slots=True)
class BackgroundTaskBuildContext:
    message_sink: BackgroundMessageSink
    wake: AgentWakePort
    output_locations: TaskOutputLocationPort
    session_id: SessionId
    result_registry: TaskResultRegistry
    owner: BackgroundTaskOwner


BackgroundTaskServiceFactory = Callable[[BackgroundTaskBuildContext], BackgroundTaskService]


__all__ = [
    "AgentWakePort",
    "BackgroundMessageSink",
    "BackgroundTaskBuildContext",
    "BackgroundTaskService",
    "BackgroundTaskServiceFactory",
    "BackgroundTaskSnapshot",
    "LocalAsyncWorkAdapter",
    "TaskOutputLocationPort",
    "TaskResultRegistry",
]
