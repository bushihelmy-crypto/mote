#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Background task schema types."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, ClassVar, Coroutine, Generic, Optional, TypeVar

from mote.contracts.foundation.errors.report import ErrorReport
from mote.contracts.task.models import AttemptId, TaskId
from mote.orchestration.background_tasks.operation import OperationOutcome
from mote.orchestration.background_tasks.status import BackgroundTaskStatus


class TaskType(str, Enum):
    """Type of background task."""

    SHELL = "shell"
    COROUTINE = "coroutine"
    AGENT = "agent"


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
PollFactory = Callable[[], Coroutine[object, object, object]]
ResultT = TypeVar("ResultT")


# ---------------------------------------------------------------------------
# BgTaskResult
# ---------------------------------------------------------------------------


class BgTaskResult(Generic[ResultT]):
    """Return type for background-capable tool functions.

    Use the named constructors (:meth:`foreground`, :meth:`background`,
    :meth:`hybrid`) — they make the mode explicit and type-safe.
    """

    def __new__(cls, *args: object, **kwargs: object) -> "BgTaskResult[ResultT]":
        if cls is BgTaskResult:
            raise TypeError("BgTaskResult must be constructed through a legal variant factory")
        return super().__new__(cls)

    # --- Named constructors ---------------------------------------------------

    @classmethod
    def foreground(cls, result: ResultT, *, command_name: str = "") -> "ForegroundBgTaskResult[ResultT]":
        """Create a foreground-only result (no background work)."""
        return ForegroundBgTaskResult(result=result, command_name=command_name)

    @classmethod
    def background(
        cls,
        poll_factory: PollFactory,
        *,
        command_name: str,
    ) -> "BackgroundBgTaskResult":
        """Create a background-only result (poll submitted, no immediate value)."""
        return BackgroundBgTaskResult(poll_factory=poll_factory, command_name=command_name)

    @classmethod
    def hybrid(
        cls,
        result: ResultT,
        poll_factory: PollFactory,
        *,
        command_name: str,
    ) -> "HybridBgTaskResult[ResultT]":
        """Create a hybrid result (immediate value AND background poll)."""
        return HybridBgTaskResult(result=result, poll_factory=poll_factory, command_name=command_name)


@dataclass(frozen=True, slots=True)
class ForegroundBgTaskResult(BgTaskResult[ResultT]):
    result: ResultT
    command_name: str = ""
    mode: ClassVar[BgTaskMode] = BgTaskMode.FOREGROUND


@dataclass(frozen=True, slots=True)
class BackgroundBgTaskResult(BgTaskResult[None]):
    poll_factory: PollFactory = field(repr=False)
    command_name: str = ""
    mode: ClassVar[BgTaskMode] = BgTaskMode.BACKGROUND


@dataclass(frozen=True, slots=True)
class HybridBgTaskResult(BgTaskResult[ResultT]):
    result: ResultT
    poll_factory: PollFactory = field(repr=False)
    command_name: str = ""
    mode: ClassVar[BgTaskMode] = BgTaskMode.HYBRID


@dataclass(frozen=True, slots=True)
class TaskSnapshot:
    """Immutable query projection; mutable pool bookkeeping never crosses its owner."""

    task_id: TaskId = TaskId("")
    attempt_id: AttemptId = field(default_factory=lambda: AttemptId(1))
    command_name: str = ""
    status: BackgroundTaskStatus = BackgroundTaskStatus.PENDING
    submit_time: float = field(default_factory=time.time)
    start_time: float = field(default_factory=time.time)
    end_time: Optional[float] = None
    result: Optional[str] = None
    error: ErrorReport | None = None
    notified: bool = False
    output_path: Optional[str] = None
    task_type: str = TaskType.COROUTINE
    task_kind: Optional[str] = None
    agent_id: Optional[str] = None
    output_capped: bool = False
    outcome: Optional[OperationOutcome] = field(default=None, repr=False)


@dataclass
class _TaskState:
    """Metadata snapshot for a background task.

    Stored by ``BackgroundTaskPool`` for every submitted task and retained
    after completion so that ``check_task`` can query finished tasks.
    """

    task_id: TaskId = TaskId("")
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

    def snapshot(self) -> TaskSnapshot:
        return TaskSnapshot(
            self.task_id,
            self.attempt_id,
            self.command_name,
            self.status,
            self.submit_time,
            self.start_time,
            self.end_time,
            self.result,
            self.error,
            self.notified,
            self.output_path,
            self.task_type,
            self.task_kind,
            self.agent_id,
            self._output_capped,
            self.outcome,
        )


@dataclass(frozen=True, slots=True)
class TaskAttemptSettlement:
    attempt_id: AttemptId
    status: BackgroundTaskStatus
    result: str | None
    error: ErrorReport | None
    ended_at: float | None
