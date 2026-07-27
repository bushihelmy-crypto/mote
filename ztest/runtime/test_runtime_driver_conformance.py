"""The shared live Runtime contract instantiated for every persistent driver."""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

import pytest

from mote.contracts.canvas import CanvasElement, CanvasOperation
from mote.runtime.tools.dependency import _browser_runtime as browser_runtime_module
from mote.runtime.tools.dependency._browser_runtime import BrowserRuntimeDriver
from mote.runtime.tools.dependency._canvas import CanvasRuntimeDriver
from mote.runtime.tools.dependency._device.runtime import DeviceRuntimeDriver
from mote.runtime.tools.dependency._kernel import KernelRuntimeDriver
from mote.runtime.tools.dependency._terminal import TerminalRuntimeDriver
from mote.ztest.executor.dependency.device_fakes import FakeDeviceBackend
from mote.ztest.runtime.runtime_driver_conformance import (
    RuntimeDriverConformanceCase,
    assert_failed_restore_is_retryable,
    assert_handoff_churn_conformance,
    assert_live_runtime_driver_conformance,
)

from .test_browser_runtime import _FakeBrowserSession


@pytest.fixture(params=("browser", "device", "terminal", "canvas", "jupyter"))
def runtime_driver_case(
    request: pytest.FixtureRequest,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> RuntimeDriverConformanceCase:
    name = str(request.param)
    driver: Any
    mutate: Callable[[Any], Awaitable[None]]

    if name == "browser":
        monkeypatch.setattr(browser_runtime_module, "BrowserSession", _FakeBrowserSession)
        driver = BrowserRuntimeDriver(session_key="conformance-browser")

        async def mutate(driver: BrowserRuntimeDriver) -> None:
            driver.surface_changed()

    elif name == "device":
        driver = DeviceRuntimeDriver(session_key="conformance-device", backend=FakeDeviceBackend())

        async def mutate(driver: DeviceRuntimeDriver) -> None:
            driver.surface_changed()

    elif name == "terminal":
        driver = TerminalRuntimeDriver(session_key="conformance-terminal", cwd=str(tmp_path))

        async def mutate(driver: TerminalRuntimeDriver) -> None:
            await driver.feed("printf conformance\\n", 1_000)

    elif name == "canvas":
        driver = CanvasRuntimeDriver()

        async def mutate(driver: CanvasRuntimeDriver) -> None:
            driver.apply(
                [
                    CanvasOperation(
                        op="upsert",
                        element=CanvasElement(id="conformance", kind="rect", x=1, y=1),
                    )
                ]
            )

    else:
        driver = KernelRuntimeDriver(session_key="conformance-jupyter", cwd=str(tmp_path))

        async def mutate(driver: KernelRuntimeDriver) -> None:
            await driver.execute("print('conformance')", 30)

    return RuntimeDriverConformanceCase(name=name, driver=driver, mutate=mutate)


@pytest.mark.asyncio
async def test_all_live_runtime_drivers_share_one_behavioral_contract(
    runtime_driver_case: RuntimeDriverConformanceCase,
) -> None:
    await assert_live_runtime_driver_conformance(runtime_driver_case)


@pytest.mark.asyncio
async def test_all_live_runtime_drivers_survive_repeated_handoff_churn(
    runtime_driver_case: RuntimeDriverConformanceCase,
) -> None:
    await assert_handoff_churn_conformance(runtime_driver_case)


@pytest.mark.asyncio
async def test_all_live_runtime_drivers_release_partial_failed_restore_resources(
    runtime_driver_case: RuntimeDriverConformanceCase,
) -> None:
    await assert_failed_restore_is_retryable(runtime_driver_case)
