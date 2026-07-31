"""Errors raised by the Artifact subsystem."""
from __future__ import annotations

from typing import ClassVar

from mote.contracts.foundation.errors.base import NonRetryableError, RetryableError
from mote.contracts.foundation.errors.codes import ErrorCode


class ArtifactNotFoundError(NonRetryableError):
    default_code: ClassVar[ErrorCode] = ErrorCode.ARTIFACT_NOT_FOUND


class ArtifactRevisionConflictError(RetryableError):
    default_code: ClassVar[ErrorCode] = ErrorCode.ARTIFACT_REVISION_CONFLICT


class ArtifactIdempotencyConflictError(NonRetryableError):
    default_code: ClassVar[ErrorCode] = ErrorCode.ARTIFACT_IDEMPOTENCY_CONFLICT


class ArtifactRetentionError(NonRetryableError):
    default_code: ClassVar[ErrorCode] = ErrorCode.ARTIFACT_RETENTION


class ArtifactPublicationTerminalError(ArtifactIdempotencyConflictError):
    """A dead-letter publication identity requires an explicit new attempt."""


__all__ = [
    "ArtifactIdempotencyConflictError",
    "ArtifactNotFoundError",
    "ArtifactPublicationTerminalError",
    "ArtifactRetentionError",
    "ArtifactRevisionConflictError",
]
