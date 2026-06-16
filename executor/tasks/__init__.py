"""Background / async task framework.

Public API surface for the agent's background task subsystem. Import from
``metagpt.executor.tasks`` rather than the internal modules:

    from metagpt.executor.tasks import BackgroundTaskPool, BgTaskResult, bg_tool
"""

from __future__ import annotations

from metagpt.executor.tasks.types import (
    BackgroundTaskNotification,
    BgStatus,
    BgTaskResult,
    TaskMeta,
    TaskType,
    is_bg_notification,
)
from metagpt.executor.tasks.pool import BackgroundTaskPool
from metagpt.executor.tasks.promotion import auto_background
from metagpt.executor.tasks.decorators import bg_tool, is_bg_tool, require_bg_complete
from metagpt.executor.tasks.disk_output import (
    DEFAULT_MAX_READ_BYTES,
    MAX_TASK_OUTPUT_BYTES,
    MAX_TASK_OUTPUT_BYTES_DISPLAY,
    DiskTaskOutput,
    TaskOutputStore,
)
from metagpt.executor.tasks.stall_detector import StallDetector
from metagpt.executor.tasks.attachment import (
    GenerateResult,
    TaskAttachment,
    TaskAttachmentGenerator,
    format_attachment_xml,
)
from metagpt.executor.tasks.turn_context_source import BackgroundTaskContextSource
from metagpt.executor.tasks.bggraph.report import (
    ProgressWriter,
    report_progress,
    set_progress_writer,
    reset_progress_writer,
)
from metagpt.executor.tasks.bggraph import (
    END,
    START,
    GraphBatchFailureError,
    BgGraph,
    GraphRecursionError,
    GraphRouterError,
    GraphState,
    LlmPauseResult,
    Stage,
)

__all__ = [
    "BgTaskResult",
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
