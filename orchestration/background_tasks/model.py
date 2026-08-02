#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Background task schema types."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Coroutine, Optional

from pydantic import field_validator

from mote.contracts.conversation import UserMessage
from mote.contracts.foundation.errors.report import ErrorReport
from mote.contracts.task.models import AttemptId
from mote.orchestration.background_tasks.operation import OperationOutcome
from mote.orchestration.background_tasks.status import BackgroundTaskStatus


class TaskType(str, Enum):
    """Type of background task."""

    SHELL = "shell"
    COROUTINE = "coroutine"
    AGENT = "agent"


class BackgroundTaskNotification(UserMessage):
    """Structured notification for background task completion.

    ``error`` carries the JSON-native :meth:`ErrorReport.as_dict` form on a
    failed task (``None`` otherwise), completing the structured-field set
    (``task_id`` / ``status`` / ``result`` / ``error``). It is stored as a plain
    dict (not the ``ErrorReport`` dataclass) so the notification serializes
    cleanly into the session rollout JSONL; the rendered ``<error>`` block also
    appears inside ``content`` for the model.

    ``task_terminal`` marks the *one* whole-task outcome (success / failed /
    timeout / cancelled / waiting-for-route) as opposed to a mid-flight node
    event. The pool's ``deliver`` choke point uses it to guarantee exactly one
    terminal reaches the agent per task: whichever producer (the in-graph
    progress writer or the out-of-band ``_on_done`` callback) delivers first
    wins, and the other's duplicate is dropped. This lets both producers call
    ``deliver`` freely without coordinating through a shared flag.
    """

    task_id: str = ""
    attempt_id: AttemptId = field(default_factory=lambda: AttemptId(1))
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


def is_bg_notification(msg) -> bool:
    """Check whether *msg* is a background-task completion notification."""
    return isinstance(msg, BackgroundTaskNotification)


# ---------------------------------------------------------------------------
# BgTaskMode / PollFactory
# ---------------------------------------------------------------------------


class BgTaskMode(str, Enum):
    """Explicit mode declaration for BgTaskResult."""

    FOREGROUND = "foreground"  # result only, no bg work
    BACKGROUND = "background"  # poll only, no immediate result
    HYBRID = "hybrid"  # result + background poll


#: A callable that returns a coroutine — instantiation is deferred to the
#: pool's ``submit()`` call site so there's no dangling coroutine to GC-warn.
PollFactory = Callable[[], Coroutine]


# ---------------------------------------------------------------------------
# BgTaskResult
# ---------------------------------------------------------------------------


@dataclass
class BgTaskResult:
    """Return type for background-capable tool functions.

    Use the named constructors (:meth:`foreground`, :meth:`background`,
    :meth:`hybrid`) — they make the mode explicit and type-safe.
    """

    mode: BgTaskMode = BgTaskMode.FOREGROUND
    result: Any = None
    poll_factory: Optional[PollFactory] = field(default=None, repr=False)
    command_name: str = ""

    # --- Named constructors ---------------------------------------------------

    @classmethod
    def foreground(cls, result: Any, *, command_name: str = "") -> "BgTaskResult":
        """Create a foreground-only result (no background work)."""
        return cls(mode=BgTaskMode.FOREGROUND, result=result, command_name=command_name)

    @classmethod
    def background(
        cls,
        poll_factory: PollFactory,
        *,
        command_name: str,
    ) -> "BgTaskResult":
        """Create a background-only result (poll submitted, no immediate value)."""
        return cls(
            mode=BgTaskMode.BACKGROUND,
            poll_factory=poll_factory,
            command_name=command_name,
        )

    @classmethod
    def hybrid(
        cls,
        result: Any,
        poll_factory: PollFactory,
        *,
        command_name: str,
    ) -> "BgTaskResult":
        """Create a hybrid result (immediate value AND background poll)."""
        return cls(
            mode=BgTaskMode.HYBRID,
            result=result,
            poll_factory=poll_factory,
            command_name=command_name,
        )


@dataclass
class TaskMeta:
    """Metadata snapshot for a background task.

    Stored by ``BackgroundTaskPool`` for every submitted task and retained
    after completion so that ``check_task`` can query finished tasks.
    """

    task_id: str = ""
    attempt_id: AttemptId = field(default_factory=lambda: AttemptId(1))
    command_name: str = ""
    status: BackgroundTaskStatus = BackgroundTaskStatus.PENDING
    submit_time: float = field(default_factory=time.time)
    start_time: float = field(default_factory=time.time)  # updated when semaphore acquired
    end_time: Optional[float] = None
    result: Optional[str] = None
    error: ErrorReport | None = None
    notified: bool = False  # True after _on_done pushes BackgroundTaskNotification
    output_path: Optional[str] = None  # disk path for task output (set by TaskOutputStore)
    task_type: str = TaskType.COROUTINE  # default coroutine
    task_kind: Optional[str] = None  # shell sub-type: "bash" / "monitor"
    agent_id: Optional[str] = None  # owning agent ID
    _output_capped: bool = False  # True when killed by disk output cap
    outcome: Optional[OperationOutcome] = field(default=None, repr=False)

    # --- push-once result survival + consume/GC ---
    # ``registered_resource`` is an idempotency guard: True once ``_on_done``'s
    # terminal callback has registered this task's push-once result as a ResourceUnit for
    # post-compaction re-projection, so a resubmit→re-terminal does not double-load.
    registered_resource: bool = False
    # ``retrieved`` marks that the model has actually *consumed* the result
    # through the owning task surface. A consumed result is
    # unloaded from the registry and its meta is eligible for reaping — the
    # "real consume" half of the double-safety (the other half is round-based reap).
    retrieved: bool = False


@dataclass(frozen=True, slots=True)
class TaskAttemptSettlement:
    attempt_id: AttemptId
    status: BackgroundTaskStatus
    result: str | None
    error: ErrorReport | None
    ended_at: float | None
