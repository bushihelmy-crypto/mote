"""Runtime non-blocking boundary for synchronous filesystem work."""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from typing import Callable, TypeVar

T = TypeVar("T")

# Filesystem and git plumbing must not compete with arbitrary CPU work submitted
# to asyncio's process-wide default executor. ThreadPoolExecutor owns and joins
# these workers at interpreter shutdown.
_DISK_EXECUTOR = ThreadPoolExecutor(max_workers=4, thread_name_prefix="mote-disk")
_COMPLETION_POLL_SECONDS = 0.001


async def run_disk_io(fn: Callable[..., T], /, *args, **kwargs) -> T:
    """Run one blocking filesystem operation outside the event-loop thread."""
    future = _DISK_EXECUTOR.submit(partial(fn, *args, **kwargs))
    # Do not bridge through asyncio.wrap_future/run_in_executor here. Some host
    # event-loop policies can lose the cross-thread completion callback during
    # loop teardown. Cooperative polling keeps completion owned by this loop.
    while not future.done():
        await asyncio.sleep(_COMPLETION_POLL_SECONDS)
    return future.result()


__all__ = ["run_disk_io"]
