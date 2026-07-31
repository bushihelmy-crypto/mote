"""Background-task exceptions.

These are *whole-task* outcomes surfaced by the pool's ``_on_done`` callback —
distinct from the graph-tier errors (which describe how a graph run failed) and
from the node-tier errors (a single node). A background task may end by being
timed out (its wall-clock budget elapsed) or cancelled (killed externally or
because its output exceeded the disk cap). Neither is a normal raised error from
inside the task — the pool synthesizes one so the terminal notification carries
the same structured :class:`~mote.contracts.foundation.errors.report.ErrorReport`
contract every other failure surface uses.
"""

from __future__ import annotations

from typing import ClassVar

from mote.contracts.foundation.errors.base import MoteError, NonRetryableError, RetryableError
from mote.contracts.foundation.errors.codes import ErrorCode


class BackgroundTaskError(MoteError):
    """Base for whole-task background-execution outcomes."""


class BackgroundTaskTimeoutError(BackgroundTaskError, RetryableError):
    """A background task exceeded its wall-clock time budget.

    Retryable in principle (the same command may finish within budget on a
    re-run), so the contract surfaces ``recovery=retry`` to the model.
    """

    default_code: ClassVar[ErrorCode] = ErrorCode.BG_TASK_TIMEOUT


class BackgroundTaskCancelledError(BackgroundTaskError, NonRetryableError):
    """A background task was cancelled — killed externally or because its
    output exceeded the disk cap. Not retried automatically.
    """

    default_code: ClassVar[ErrorCode] = ErrorCode.BG_TASK_CANCELLED
