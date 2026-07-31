"""Shared failure policy for durable reconciliation workers."""
from __future__ import annotations

import sqlite3

from mote.contracts.artifact.errors import ArtifactRevisionConflictError
from mote.contracts.foundation.errors.base import NonRetryableError, RetryableError

MAX_RECONCILIATION_ATTEMPTS = 8


def is_retryable_reconciliation_error(exc: BaseException) -> bool:
    """Classify only failures whose unchanged durable input may later succeed."""
    if isinstance(exc, (NonRetryableError, ArtifactRevisionConflictError)):
        return False
    if isinstance(exc, RetryableError):
        return True
    return isinstance(
        exc,
        (TimeoutError, ConnectionError, sqlite3.OperationalError, OSError),
    )


__all__ = ["MAX_RECONCILIATION_ATTEMPTS", "is_retryable_reconciliation_error"]
