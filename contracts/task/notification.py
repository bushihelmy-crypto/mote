"""Canonical process-local background-task notification message."""

from __future__ import annotations

from typing import Optional

from pydantic import field_validator

from mote.contracts.conversation import UserMessage
from mote.contracts.foundation.errors.report import ErrorReport
from mote.contracts.task.models import AttemptId, TaskId


class BackgroundTaskNotification(UserMessage):
    task_id: TaskId = TaskId("")
    attempt_id: AttemptId = AttemptId(1)
    command_name: str = ""
    status: str = ""
    result: Optional[str] = None
    error: Optional[dict] = None
    task_terminal: bool = False

    @field_validator("error", mode="before")
    @classmethod
    def validate_error_envelope(cls, value: object) -> dict[str, object] | None:
        if value is None:
            return None
        return ErrorReport.from_dict(value).as_dict()


def is_background_task_notification(message: object) -> bool:
    return isinstance(message, BackgroundTaskNotification)


__all__ = ["BackgroundTaskNotification", "is_background_task_notification"]
