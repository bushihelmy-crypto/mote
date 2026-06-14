#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Background task schema types."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Coroutine, Optional

from metagpt.common.schema.messages import UserMessage


class BgStatus(str, Enum):
    """Status values for background tasks."""

    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMEOUT = "timeout"


class TaskType(str, Enum):
    """Type of background task."""

    SHELL = "shell"
    COROUTINE = "coroutine"
    AGENT = "agent"


class BackgroundTaskNotification(UserMessage):
    """Structured notification for background task completion."""

    task_id: str = ""
    command_name: str = ""
    status: str = ""
    result: Optional[str] = None


def is_bg_notification(msg) -> bool:
    """Check whether *msg* is a background-task completion notification."""
    return isinstance(msg, BackgroundTaskNotification)


# ---------------------------------------------------------------------------
# BgTaskResult / TaskMeta (from tasks/result.py)
# ---------------------------------------------------------------------------


@dataclass
class BgTaskResult:
    """Return type for background-capable tool functions.

    Attributes:
        result: Immediate value returned to the LLM (e.g. initial status).
        poll: If not *None*, a coroutine that will be submitted to
            ``BackgroundTaskPool`` for background polling.
        command_name: Human-readable label used in the completion notification.
    """

    result: Any = None
    poll: Optional[Coroutine] = field(default=None, repr=False)
    command_name: str = ""


@dataclass
class TaskMeta:
    """Metadata snapshot for a background task.

    Stored by ``BackgroundTaskPool`` for every submitted task and retained
    after completion so that ``check_task`` can query finished tasks.
    """

    task_id: str = ""
    command_name: str = ""
    status: str = BgStatus.PENDING
    submit_time: float = field(default_factory=time.time)
    start_time: float = field(default_factory=time.time)  # updated when semaphore acquired
    end_time: Optional[float] = None
    result: Optional[str] = None
    notified: bool = False  # True after _on_done pushes BackgroundTaskNotification
    output_path: Optional[str] = None  # disk path for task output (set by TaskOutputStore)
    task_type: str = TaskType.COROUTINE  # default coroutine, backward compatible
    task_kind: Optional[str] = None  # shell sub-type: "bash" / "monitor"
    agent_id: Optional[str] = None  # owning agent ID
    _output_capped: bool = False  # True when killed by disk output cap
