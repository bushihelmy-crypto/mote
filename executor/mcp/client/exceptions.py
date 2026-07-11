"""MCP client exception"""
from __future__ import annotations

import functools
from typing import Any, Callable, TypeVar

# NonRetryableToolError now lives in the global exception system; re-exported
# here so existing imports/raises keep working unchanged.
from mote.common.exception import NonRetryableToolError, is_retryable  # noqa: F401

ReturnType = TypeVar("ReturnType")


def extract_meaningful_error(exception) -> Exception | None:
    """Extract meaningful errors from ExceptionGroup.

    Args:
        exception: The exception object to check

    Returns:
        Exception|None: The first meaningful exception found, or None if no meaningful error
    """
    if not hasattr(exception, "exceptions"):
        return exception

    # For ExceptionGroup, extract the first meaningful exception
    if hasattr(exception, "exceptions") and exception.exceptions:
        if len(exception.exceptions) == 1:
            return extract_meaningful_error(exception.exceptions[0])

        for exc in exception.exceptions:
            # Recursively check nested ExceptionGroups
            meaningful_error = extract_meaningful_error(exc)
            if meaningful_error and not hasattr(meaningful_error, "exceptions"):
                return meaningful_error

    return None


def extract_tool_error(exception) -> NonRetryableToolError | None:
    """Check if an exception or its nested exceptions contain a NonRetryableToolError.

    This function is kept for backward compatibility with retry logic.
    """
    # Check current exception directly
    if isinstance(exception, NonRetryableToolError):
        return exception

    # Check nested exceptions in ExceptionGroup
    if hasattr(exception, "exceptions"):
        for exc in exception.exceptions:
            tool_error = extract_tool_error(exc)
            if tool_error:
                return tool_error

    return None


def retry_if_retryable_error(retry_state):
    """Determine if an exception should trigger a retry attempt.

    This function is used with tenacity's retry decorator to control retry behavior.

    Args:
        retry_state: RetryCallState object containing information about the current retry state
                    and the outcome of the last attempt

    Returns:
        bool:
            - True: The exception should be retried (temporary failure)
            - False: The exception should NOT be retried (permanent failure)
    """
    exception = retry_state.outcome.exception()

    if exception is None:
        return False

    # Unwrap ExceptionGroups to the first meaningful error, then decide on
    # semantics via the global ``is_retryable`` predicate.
    meaningful = extract_meaningful_error(exception) or exception
    return is_retryable(meaningful)


def handle_exception_group(func: Callable[..., Any]) -> Callable[..., Any]:
    """Extract meaningful errors from ExceptionGroup when they occur.

    Only for async functions.

    This decorator extracts the actual error from ExceptionGroup, such as:
    - ConnectError('All connection attempts failed')
    - NonRetryableToolError('Tool not found')
    - Any other meaningful exception
    """

    @functools.wraps(func)
    async def async_wrapper(*args, **kwargs):
        try:
            return await func(*args, **kwargs)
        except Exception as e:
            # Try to extract meaningful error from ExceptionGroup
            meaningful_error = extract_meaningful_error(e)
            if meaningful_error and meaningful_error is not e:
                raise meaningful_error from None
            raise

    return async_wrapper
