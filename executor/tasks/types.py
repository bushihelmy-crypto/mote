#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Background task schema types."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Awaitable, Callable, Coroutine, Optional

from metagpt.common.schema.messages import UserMessage
from metagpt.common.schema.node_status import BgStatus


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
    command_name: str = ""
    status: str = ""
    result: Optional[str] = None
    error: Optional[dict] = None
    task_terminal: bool = False


def is_bg_notification(msg) -> bool:
    """Check whether *msg* is a background-task completion notification."""
    return isinstance(msg, BackgroundTaskNotification)


# ---------------------------------------------------------------------------
# BgTaskMode / GraphMeta / PollFactory
# ---------------------------------------------------------------------------


class BgTaskMode(str, Enum):
    """Explicit mode declaration for BgTaskResult."""

    FOREGROUND = "foreground"  # result only, no bg work
    BACKGROUND = "background"  # poll only, no immediate result
    HYBRID = "hybrid"  # result + background poll


#: A callable that returns a coroutine — instantiation is deferred to the
#: pool's ``submit()`` call site so there's no dangling coroutine to GC-warn.
PollFactory = Callable[[], Coroutine]


@dataclass
class GraphMeta:
    """Graph restart/resume metadata — only populated by BgGraph pipelines.

    Tool authors never touch this; only the BgGraph engine sets it.
    """

    graph_ref: Any = None
    initial_params: Optional[dict] = None
    factory: Optional[Callable[..., Awaitable["BgTaskResult"]]] = None
    # Authoritative per-node execution records (GraphRunState). The driver
    # mutates this same object as it runs; the pool snapshots it onto TaskMeta
    # so resume reads true node status instead of inferring it from state values.
    run_state: Any = None
    # Live graph state the driver mutates in place. Carried here (like run_state)
    # so the pool can snapshot it onto TaskMeta even on a timeout — where the
    # bare asyncio.TimeoutError, raised outside the driver, carries no state.
    state: Any = None


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
    graph_meta: Optional[GraphMeta] = field(default=None, repr=False)

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
        graph_meta: Optional[GraphMeta] = None,
    ) -> "BgTaskResult":
        """Create a background-only result (poll submitted, no immediate value)."""
        return cls(
            mode=BgTaskMode.BACKGROUND,
            poll_factory=poll_factory,
            command_name=command_name,
            graph_meta=graph_meta,
        )

    @classmethod
    def hybrid(
        cls,
        result: Any,
        poll_factory: PollFactory,
        *,
        command_name: str,
        graph_meta: Optional[GraphMeta] = None,
    ) -> "BgTaskResult":
        """Create a hybrid result (immediate value AND background poll)."""
        return cls(
            mode=BgTaskMode.HYBRID,
            result=result,
            poll_factory=poll_factory,
            command_name=command_name,
            graph_meta=graph_meta,
        )


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
    error: Optional[dict] = None  # ErrorReport.as_dict form on a FAILED task
    notified: bool = False  # True after _on_done pushes BackgroundTaskNotification
    output_path: Optional[str] = None  # disk path for task output (set by TaskOutputStore)
    task_type: str = TaskType.COROUTINE  # default coroutine, backward compatible
    task_kind: Optional[str] = None  # shell sub-type: "bash" / "monitor"
    agent_id: Optional[str] = None  # owning agent ID
    _output_capped: bool = False  # True when killed by disk output cap

    # --- graph resume support ---
    graph_meta: Optional[GraphMeta] = field(default=None, repr=False)
    state_snapshot: Optional[Any] = field(default=None, repr=False)
    completed_nodes: set = field(default_factory=set)
    # Authoritative per-node execution records (GraphRunState), captured on pause
    # AND on failure. The truth source for resume; queried by GetNodeState.
    run_state: Optional[Any] = field(default=None, repr=False)
    retry_count: int = 0
    max_restarts: int = 3
