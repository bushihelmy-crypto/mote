"""Background / async task framework.

Public API surface for the agent's background task subsystem. Import from
``mote.executor.tasks`` rather than the internal modules:

    from mote.executor.tasks import BackgroundTaskPool, BgTaskResult, bg_tool
"""

from __future__ import annotations

from mote.executor.tasks.attachment import (
    GenerateResult,
    TaskAttachment,
    TaskAttachmentGenerator,
    format_attachment_xml,
)
from mote.executor.tasks.bggraph import (
    END,
    START,
    BgGraph,
    GraphBatchFailureError,
    GraphRecursionError,
    GraphRouterError,
    GraphState,
    LlmPauseResult,
    Stage,
)
from mote.executor.tasks.bggraph.report import (
    ProgressWriter,
    report_progress,
    reset_progress_writer,
    set_progress_writer,
)
from mote.executor.tasks.decorators import bg_tool, is_bg_tool, require_bg_complete
from mote.executor.tasks.disk_output import (
    DEFAULT_MAX_READ_BYTES,
    MAX_TASK_OUTPUT_BYTES,
    MAX_TASK_OUTPUT_BYTES_DISPLAY,
    DiskTaskOutput,
    TaskOutputStore,
)
from mote.executor.tasks.pool import BackgroundTaskPool
from mote.executor.tasks.promotion import auto_background
from mote.executor.tasks.stall_detector import StallDetector
from mote.executor.tasks.turn_context_source import BackgroundTaskContextSource
from mote.executor.tasks.types import (
    BackgroundTaskNotification,
    BgStatus,
    BgTaskMode,
    BgTaskResult,
    GraphMeta,
    PollFactory,
    TaskMeta,
    TaskType,
    is_bg_notification,
)

__all__ = [
    "BgTaskResult",
    "BgTaskMode",
    "GraphMeta",
    "PollFactory",
    "TaskMeta",
    "TaskType",
    "BackgroundTaskNotification",
    "is_bg_notification",
    "BgStatus",
    "BackgroundTaskPool",
    "auto_background",
    "bg_tool",
    "is_bg_tool",
    "require_bg_complete",
    "DiskTaskOutput",
    "TaskOutputStore",
    "MAX_TASK_OUTPUT_BYTES",
    "MAX_TASK_OUTPUT_BYTES_DISPLAY",
    "DEFAULT_MAX_READ_BYTES",
    "StallDetector",
    "TaskAttachment",
    "TaskAttachmentGenerator",
    "GenerateResult",
    "format_attachment_xml",
    "BackgroundTaskContextSource",
    "ProgressWriter",
    "report_progress",
    "set_progress_writer",
    "reset_progress_writer",
    "BgGraph",
    "GraphState",
    "Stage",
    "LlmPauseResult",
    "START",
    "END",
    "GraphRouterError",
    "GraphRecursionError",
    "GraphBatchFailureError",
]
