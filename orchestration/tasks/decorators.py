"""Decorators for background-task-aware tool functions."""

from __future__ import annotations

import functools
from typing import Callable, Optional

from mote.contracts.text import count_noun
from mote.orchestration.tasks.pool import BackgroundTaskPool
from mote.runtime.reporting import ThoughtReporter


def require_bg_complete(
    pool_getter: Callable[[], Optional[BackgroundTaskPool]],
) -> Callable:
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

    def decorator(fn: Callable) -> Callable:
        @functools.wraps(fn)
        async def wrapper(*args, **kwargs):
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


def bg_tool(fn: Callable) -> Callable:
    """Decorator: mark a tool function for automatic background dispatch.

    Every invocation of a ``@bg_tool``-decorated function is routed
    directly to ``BackgroundTaskPool`` by the framework.  The LLM
    receives an immediate acknowledgment (task_id) instead of waiting
    for the result.

    The decorator sets a ``_bg_tool`` marker attribute on the wrapped
    function.  ``Role._run_command`` checks for this marker and
    calls ``_run_command_in_background`` automatically — the tool
    itself runs exactly as before, unaware of background scheduling.

    Usage::

        @bg_tool
        async def generate_videos(self, prompt: str) -> str:
            ...
    """
    fn._bg_tool = True
    return fn


def is_bg_tool(fn: object) -> bool:
    """Return *True* if *fn* was decorated with :func:`bg_tool`."""
    return getattr(fn, "_bg_tool", False) is True
