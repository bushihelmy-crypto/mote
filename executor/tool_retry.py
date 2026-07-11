#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Opt-in retry decorator for tool ``call`` methods.

The executor catches every ``ToolError`` and turns it into
``ToolResult(success=False)`` (see ``ToolExecutor.run_command``); a plain
``ToolError`` therefore aborts on its first raise. That is the right default —
most tool failures (bad path, validation, not-found) are not worth retrying.

Some tool failures ARE transient (a network blip, a rate-limited upstream, a
temporary lock). For those a tool can ``raise RetryableToolError(...)`` AND wrap
its ``call`` with this decorator so the *same* call is retried before the error
is allowed to bubble up to ``run_command``.

Only the decorated ``call`` retries — undecorated tools keep today's behaviour.
And only a *retryable* error triggers a retry: the predicate defaults to
``is_retryable`` (so ``RetryableToolError`` retries while a plain ``ToolError``
does not, since it is a ``NonRetryableError``). After ``max_attempts`` the last
exception is re-raised unchanged, so ``run_command`` still converts it into
``ToolResult(success=False)``.

This module only provides the mechanism; no concrete tool is wired here.
"""
from __future__ import annotations

from typing import Callable, Optional, Tuple, Type, Union

from mote.common.exception import is_retryable
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_random_exponential

# Either a predicate ``(exc) -> bool`` or an exception type / tuple of types.
RetryOn = Union[Callable[[BaseException], bool], Type[BaseException], Tuple[Type[BaseException], ...]]


def retryable_tool(
    *,
    max_attempts: int = 3,
    max_wait: float = 10.0,
    retry_on: Optional[RetryOn] = None,
):
    """Decorate an (async or sync) tool ``call`` to auto-retry transient failures.

    Args:
        max_attempts: Total attempts including the first; after this the last
            exception is re-raised.
        max_wait: Cap (seconds) for the random-exponential backoff between tries.
        retry_on: Override the retry predicate. Pass a callable ``(exc) -> bool``
            or an exception type / tuple of types. Defaults to ``is_retryable``,
            so ``RetryableToolError`` retries and a plain ``ToolError`` does not.

    Returns:
        A tenacity ``@retry`` decorator (``reraise=True``), so on exhaustion the
        original exception propagates — letting ``run_command`` still produce
        ``ToolResult(success=False)``.
    """
    if retry_on is None:
        predicate: Callable[[BaseException], bool] = is_retryable
    elif callable(retry_on) and not isinstance(retry_on, type):
        predicate = retry_on
    else:
        exc_types = retry_on
        predicate = lambda exc: isinstance(exc, exc_types)  # noqa: E731

    return retry(
        retry=retry_if_exception(predicate),
        stop=stop_after_attempt(max_attempts),
        wait=wait_random_exponential(multiplier=1, max=max_wait),
        reraise=True,
    )
