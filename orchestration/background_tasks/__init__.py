"""Background / async task framework.

Public API surface for the agent's background task subsystem. Import from
``mote.orchestration.background_tasks`` rather than the internal modules:

    from mote.orchestration.background_tasks import BackgroundTaskPool, BgTaskResult, bg_tool
"""

from __future__ import annotations

from mote.orchestration.background_tasks.decorators import bg_tool, is_bg_tool, require_bg_complete
from mote.orchestration.background_tasks.model import (
    BackgroundTaskNotification,
    BgStatus,
    BgTaskMode,
    BgTaskResult,
    PollFactory,
    TaskMeta,
    TaskType,
    is_bg_notification,
)
from mote.orchestration.background_tasks.monitoring.stall import StallDetector
from mote.orchestration.background_tasks.monitoring.turn_context import BackgroundTaskContextSource
from mote.orchestration.background_tasks.operation import (
    CoroutineOperation,
    DeferredOperation,
    OperationCancelled,
    OperationFailed,
    OperationOutcome,
    OperationPaused,
    OperationSucceeded,
    OperationTimedOut,
    ResumeRef,
    StopDisposition,
    StopReason,
)
from mote.orchestration.background_tasks.pool import BackgroundTaskPool
from mote.orchestration.background_tasks.promotion import auto_background
from mote.orchestration.background_tasks.results.attachment import (
    GenerateResult,
    TaskAttachment,
    TaskAttachmentGenerator,
    format_attachment_xml,
)
from mote.orchestration.background_tasks.results.store import (
    DEFAULT_MAX_READ_BYTES,
    MAX_TASK_OUTPUT_BYTES,
    MAX_TASK_OUTPUT_BYTES_DISPLAY,
    DiskTaskOutput,
    TaskOutputStore,
)

__all__ = [
    "BgTaskResult",
    "BgTaskMode",
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
    "CoroutineOperation",
    "DeferredOperation",
    "OperationCancelled",
    "OperationFailed",
    "OperationOutcome",
    "OperationPaused",
    "OperationSucceeded",
    "OperationTimedOut",
    "ResumeRef",
    "StopDisposition",
    "StopReason",
]
