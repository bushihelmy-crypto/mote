"""Decorators for background-task-aware tool functions."""

from __future__ import annotations

import functools
from typing import Awaitable, Callable, Optional, ParamSpec, TypeVar

from mote.orchestration.background_tasks.pool import BackgroundTaskPool
from mote.runtime.presentation import count_noun
from mote.runtime.telemetry.reporting import ThoughtReporter

P = ParamSpec("P")
R = TypeVar("R")


def require_bg_complete(
    pool_getter: Callable[[], Optional[BackgroundTaskPool]],
) -> Callable[[Callable[P, Awaitable[R]]], Callable[P, Awaitable[R]]]:
    """Decorator: wait for all background tasks to finish before calling *fn*.

    Args:
        pool_getter: A zero-arg callable that returns the current
            ``BackgroundTaskPool`` (or ``None`` if not initialized).

    Usage::

        @require_bg_complete(lambda: self._bg_pool)
        async def check_ui(instruction: str) -> str:
            ...

        # or wrap an existing function:
        wrapped = require_bg_complete(lambda: self._bg_pool)(check_ui_instance.run)
    """

    def decorator(fn: Callable[P, Awaitable[R]]) -> Callable[P, Awaitable[R]]:
        @functools.wraps(fn)
        async def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            pool = pool_getter()
            if pool and pool.has_pending():
                pending = pool.pending_count
                # Collect command names of pending tasks for reporting
                task_names = []
                for tid in pool.pending_ids:
                    meta = pool.get_task_info(tid)
                    if meta and meta.command_name:
                        task_names.append(meta.command_name)
                tasks_desc = ", ".join(task_names) if task_names else count_noun(pending, "task")
                # Push waiting status to frontend
                reporter = ThoughtReporter()
                await reporter.async_report(
                    {
                        "type": "waiting_bg_tasks",
                        "message": f"Waiting for background tasks before executing {fn.__name__}: {tasks_desc}",
                        "pending_count": pending,
                        "pending_tasks": task_names,
                        "target_tool": fn.__name__,
                    },
                    "object",
                )
                await pool.wait_all()
            return await fn(*args, **kwargs)

        return wrapper

    return decorator
