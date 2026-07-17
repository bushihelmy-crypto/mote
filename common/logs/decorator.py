#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Decorator logging: :func:`log_call`.

Wraps a function so each invocation logs entry (args), exit (result + elapsed)
and exceptions. Works on both sync and async functions, supports ``@log_call``
and ``@log_call(...)``, and uses ``logger.opt(depth=1)`` so the recorded source
location points at the call site rather than this module. The current trace_id
is stamped automatically by the core logger patcher.

Note:
    Not intended for generator / async-generator functions: the wrapper would
    log "returned a generator object" before iteration and miss the real
    runtime. :class:`~mote.common.logs.mixin.LoggedMixin` skips such methods.
"""

from __future__ import annotations

import asyncio
import functools
import time
from typing import Callable, Optional

from mote.common.logs.core import logger

# Marker attribute set on wrapped callables, used by the mixin to avoid
# double-wrapping a method that is already decorated.
_LOGGED_MARKER = "_logged"


def _truncate(obj, max_len: int) -> str:
    s = repr(obj)
    return s if len(s) <= max_len else s[:max_len] + "...(truncated)"


def _fmt_args(args, kwargs, max_len: int) -> str:
    parts = [_truncate(a, max_len) for a in args]
    parts += [f"{k}={_truncate(v, max_len)}" for k, v in kwargs.items()]
    return ", ".join(parts)


def log_call(
    _func: Optional[Callable] = None,
    *,
    level: str = "INFO",
    log_args: bool = True,
    log_result: bool = True,
    log_time: bool = True,
    log_exc: bool = True,
    name: Optional[str] = None,
    max_len: int = 9999999,
) -> Callable:
    """Log entry, exit and exceptions of *func*.

    Args:
        level: Level used for the exit log line.
        log_args: Log the call arguments on entry (DEBUG).
        log_result: Include the return value on exit.
        log_time: Include the elapsed time (ms) on exit.
        log_exc: On exception, ``logger.exception`` the traceback before re-raising.
        name: Label for log lines; defaults to ``func.__qualname__``.
        max_len: Truncation length for repr'd args / result.

    Usage::

        @log_call
        def f(x): ...

        @log_call(level="DEBUG", max_len=200)
        async def g(x): ...
    """

    def decorator(func: Callable) -> Callable:
        label = name or func.__qualname__

        def _enter(args, kwargs):
            # depth=2: _enter -> wrapper -> call site.
            if log_args:
                logger.opt(depth=2).debug(f"\u2192 {label}({_fmt_args(args, kwargs, max_len)})")

        def _exit(result, elapsed_ms):
            parts = [f"\u2190 {label}"]
            if log_result:
                parts.append(f" -> {_truncate(result, max_len)}")
            if log_time:
                parts.append(f" ({elapsed_ms:.1f}ms)")
            # depth=2: _exit -> wrapper -> call site.
            logger.opt(depth=2).log(level, "".join(parts))

        @functools.wraps(func)
        def sync_wrapper(*args, **kwargs):
            _enter(args, kwargs)
            t0 = time.perf_counter()
            try:
                result = func(*args, **kwargs)
            except Exception:
                if log_exc:
                    logger.opt(depth=1).exception(f"\u2717 {label} raised")
                raise
            _exit(result, (time.perf_counter() - t0) * 1000)
            return result

        @functools.wraps(func)
        async def async_wrapper(*args, **kwargs):
            _enter(args, kwargs)
            t0 = time.perf_counter()
            try:
                result = await func(*args, **kwargs)
            except Exception:
                if log_exc:
                    logger.opt(depth=1).exception(f"\u2717 {label} raised")
                raise
            _exit(result, (time.perf_counter() - t0) * 1000)
            return result

        wrapper = async_wrapper if asyncio.iscoroutinefunction(func) else sync_wrapper
        setattr(wrapper, _LOGGED_MARKER, True)
        return wrapper

    return decorator(_func) if _func else decorator
