import asyncio
import time

import pytest

from mote.runtime.persistence.async_io import run_disk_io


@pytest.mark.asyncio
async def test_disk_io_polling_does_not_starve_event_loop_timers():
    heartbeat = asyncio.Event()

    async def tick() -> None:
        await asyncio.sleep(0.01)
        heartbeat.set()

    task = asyncio.create_task(tick())
    await run_disk_io(time.sleep, 0.05)
    await task

    assert heartbeat.is_set()
