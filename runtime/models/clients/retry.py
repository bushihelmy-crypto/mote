"""Request-local retry delay calculation for ``AttemptOrchestrator``."""

from __future__ import annotations

# Never sleep longer than this even if a provider advertises a larger cool-off —
# a misconfigured/hostile header must not park a turn for hours.
MAX_RETRY_AFTER_SECONDS = 300.0


def retry_delay(exc: BaseException, attempt: int) -> float:
    """Return the bounded delay before the next request-level attempt."""

    retry_after = getattr(exc, "retry_after", None)
    if isinstance(retry_after, (int, float)) and retry_after > 0:
        return min(float(retry_after), MAX_RETRY_AFTER_SECONDS)
    return min(float(2 ** max(attempt - 1, 0)), 60.0)
