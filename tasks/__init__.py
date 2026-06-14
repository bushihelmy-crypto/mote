"""Background / async task framework.

Public API surface for the agent's background task subsystem. Import from
``metagpt.tasks`` rather than the internal modules:

    from tasks import BackgroundTaskPool, BgTaskResult, bg_tool
"""

from __future__ import annotations

from metagpt.common.schema import BgTaskResult, TaskMeta
from metagpt.tasks.pool import BackgroundTaskPool
from metagpt.tasks.promotion import auto_background
from metagpt.tasks.decorators import bg_tool, is_bg_tool, require_bg_complete
from metagpt.tasks.disk_output import (
    DEFAULT_MAX_READ_BYTES,
    MAX_TASK_OUTPUT_BYTES,
    MAX_TASK_OUTPUT_BYTES_DISPLAY,
    DiskTaskOutput,
    TaskOutputStore,
)
from metagpt.tasks.stall_detector import StallDetector
from metagpt.tasks.attachment import (
    GenerateResult,
    TaskAttachment,
    TaskAttachmentGenerator,
    format_attachment_xml,
)

__all__ = [
    "BgTaskResult",
    "TaskMeta",
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
]
