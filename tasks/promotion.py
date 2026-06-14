"""Foreground → background auto-promotion for slow tool coroutines."""

from __future__ import annotations

import asyncio
from typing import Any, Coroutine, Optional

from metagpt.tasks.pool import BackgroundTaskPool
from metagpt.common.schema import BgTaskResult

_DEFAULT_TASK_TIMEOUT = 600.0  # 10 minutes per task
_DEFAULT_FOREGROUND_TIMEOUT = 30.0  # seconds before auto-backgrounding


async def auto_background(
    coro: Coroutine,
    pool: BackgroundTaskPool,
    command_name: str,
    foreground_timeout: float = _DEFAULT_FOREGROUND_TIMEOUT,
    task_timeout: Optional[float] = _DEFAULT_TASK_TIMEOUT,
) -> Any:
    """Run *coro* in the foreground; promote to background if it is too slow.

    1. Apply *task_timeout* (total wall-clock limit) to the coroutine.
    2. Wait up to *foreground_timeout* seconds for the coroutine to finish.
    3. If it finishes in time → return the result directly (or re-raise).
    4. If not → adopt the still-running task into *pool* and return a
       ``BgTaskResult`` whose ``result`` tells the LLM the task was moved
       to the background.

    Args:
        coro: The coroutine to execute.
        pool: The ``BackgroundTaskPool`` that will manage the task if it
            is promoted to the background.
        command_name: Human-readable label for notifications.
        foreground_timeout: Maximum seconds to wait in the foreground
            before promoting. Defaults to 5 s.
        task_timeout: Total wall-clock timeout applied to *coro*
            regardless of foreground/background. ``None`` disables the
            limit. Defaults to 600 s.

    Returns:
        The coroutine's return value if it finishes in the foreground,
        or a ``BgTaskResult`` if promoted to the background.

    Raises:
        Any exception raised by *coro* if it fails during the foreground
        window.
    """
    if task_timeout is not None and task_timeout > 0:
        effective: Coroutine = asyncio.wait_for(coro, timeout=task_timeout)
    else:
        effective = coro

    task = asyncio.create_task(effective)

    done, _ = await asyncio.wait({task}, timeout=max(foreground_timeout, 0))

    if done:
        # Completed (or failed) within the foreground window.
        return task.result()

    # Still running — promote to background.
    task_id = pool.adopt(task, command_name=command_name)
    return BgTaskResult(
        result=(
            f"Task exceeded {foreground_timeout}s foreground limit, "
            f"moved to background as {task_id} ({command_name}). "
            f"You will be notified upon completion."
        ),
        command_name=command_name,
    )
