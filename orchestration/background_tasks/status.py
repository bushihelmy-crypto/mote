"""Process-local BackgroundTask lifecycle status.

This is a LEAF module (stdlib only), safe to import from anywhere without
risking circular imports.
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import Enum

from mote.contracts.task.status import ExecutionStatusProjection, ExecutionStatusSource


class BackgroundTaskStatus(str, Enum):
    """Status owned exclusively by one Agent's process-local task pool."""

    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMEOUT = "timeout"
    SKIPPED = "skipped"


# Whole-task *terminal* statuses. Single authoritative source so the attachment generator,
# the push-once result registration, and any reap gate all agree on "done".
TERMINAL_STATUSES = frozenset(
    {
        BackgroundTaskStatus.SUCCESS,
        BackgroundTaskStatus.FAILED,
        BackgroundTaskStatus.CANCELLED,
        BackgroundTaskStatus.TIMEOUT,
    }
)


def project_background_task_status(
    status: BackgroundTaskStatus,
) -> ExecutionStatusProjection:
    return ExecutionStatusProjection(
        ExecutionStatusSource.BACKGROUND_TASK,
        status.value,
    )


def decode_background_task_status(
    payload: Mapping[str, object],
) -> BackgroundTaskStatus:
    projection = ExecutionStatusProjection.from_payload(
        payload,
        expected_source=ExecutionStatusSource.BACKGROUND_TASK,
    )
    return BackgroundTaskStatus(projection.value)
