from __future__ import annotations

import asyncio

import pytest

from mote.runtime.interactive.observation import SurfaceObservationHub


@pytest.mark.asyncio
async def test_observer_waits_for_a_new_sequence_and_detach_ends_waiting():
    hub = SurfaceObservationHub()
    sequence = 4
    hub.attach("viewer")

    changed = asyncio.create_task(hub.wait_for_change("viewer", sequence, lambda: sequence))
    await asyncio.sleep(0)
    sequence += 1
    hub.notify()

    assert await asyncio.wait_for(changed, timeout=1) is True

    detached = asyncio.create_task(hub.wait_for_change("viewer", sequence, lambda: sequence))
    await asyncio.sleep(0)
    hub.detach("viewer")

    assert await asyncio.wait_for(detached, timeout=1) is False


@pytest.mark.asyncio
async def test_close_releases_every_observer():
    hub = SurfaceObservationHub()
    hub.attach("one")
    hub.attach("two")
    waits = [asyncio.create_task(hub.wait_for_change(token, 0, lambda: 0)) for token in ("one", "two")]
    await asyncio.sleep(0)
    hub.close()

    assert await asyncio.gather(*waits) == [False, False]


@pytest.mark.asyncio
async def test_sampling_clock_runs_only_while_an_observer_is_attached():
    hub = SurfaceObservationHub()
    sequence = 0

    def advance():
        nonlocal sequence
        sequence += 1

    hub.attach("viewer")
    hub.start_sampling(advance, interval_seconds=0.01)
    changed = await asyncio.wait_for(
        hub.wait_for_change("viewer", sequence, lambda: sequence),
        timeout=1,
    )
    assert changed is True
    assert sequence > 0

    hub.detach("viewer")
    stopped_at = sequence
    await asyncio.sleep(0.03)
    assert sequence == stopped_at
