from __future__ import annotations

import asyncio
import json

import pytest

from mote.contracts.handoff import HandoffRequest, HandoffStatus, HumanHandoffOutcome
from mote.contracts.runtimes import RuntimeRef
from mote.contracts.surfaces import SurfaceInput, SurfacePresentationMode
from mote.runtime.tools.dependency._device.runtime import DeviceRuntimeDriver
from mote.ztest.executor.dependency.device_fakes import FakeDeviceBackend


@pytest.mark.asyncio
async def test_device_handoff_keeps_observation_after_input_authority_expires():
    backend = FakeDeviceBackend()
    driver = DeviceRuntimeDriver(session_key="device-runtime-test", backend=backend)
    await driver.start()
    handle = await driver.prepare_handoff(HandoffRequest(runtime_ref=RuntimeRef(runtime_id="device-1", kind="device")))
    try:
        assert handle.surface.presentation is SurfacePresentationMode.WINDOW
        initial = await driver.snapshot_surface(handle)
        payload = json.loads(initial.content)
        assert payload["width"] == 1080
        assert payload["screenshot_b64"]

        await driver.send_surface_input(handle, SurfaceInput(kind="device.tap", data='{"x":12,"y":34}'))
        assert ("tap", 12, 34) in backend.calls
        await driver.send_surface_input(handle, SurfaceInput(kind="device.text", data="hello"))
        await driver.send_surface_input(handle, SurfaceInput(kind="device.key", data="BACK"))
        await driver.send_surface_input(
            handle,
            SurfaceInput(kind="device.long_press", data='{"x":40,"y":50}'),
        )
        await driver.send_surface_input(
            handle,
            SurfaceInput(kind="device.swipe", data='{"x":10,"y":20,"x2":30,"y2":40}'),
        )
        assert ("input_text", "hello") in backend.calls
        assert ("key", "BACK") in backend.calls
        assert ("long_press", 40, 50, 800) in backend.calls
        assert ("swipe", 10, 20, 30, 40, 300) in backend.calls
        sequence = (await driver.snapshot_surface(handle)).sequence
        await driver.finish_handoff(handle, HumanHandoffOutcome(status=HandoffStatus.COMPLETED))

        update = asyncio.create_task(driver.next_surface_frame(handle, sequence))
        await backend.key("HOME")
        driver.surface_changed()
        observed = await asyncio.wait_for(update, timeout=1)
        assert observed is not None
        assert observed.sequence == sequence + 1

        with pytest.raises(RuntimeError, match="handoff handle"):
            await driver.send_surface_input(handle, SurfaceInput(kind="device.key", data="BACK"))
        await driver.detach_surface(handle)
    finally:
        await driver.aclose()

    assert backend.shut is True
