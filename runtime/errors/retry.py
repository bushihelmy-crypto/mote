"""Retry terminal-state policies."""

from tenacity import RetryCallState

from mote.runtime.telemetry.logging import logger


def log_and_reraise(retry_state: RetryCallState) -> None:
    outcome = retry_state.outcome
    assert outcome is not None, "retry callback ran before an attempt completed"
    exc = outcome.exception()
    assert exc is not None, "retry callback ran after a successful attempt"
    logger.error(f"Retry attempts exhausted. Last exception: {exc}")
    raise exc


__all__ = ["log_and_reraise"]
