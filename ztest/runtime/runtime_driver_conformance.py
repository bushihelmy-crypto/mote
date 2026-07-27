"""Reusable behavioral contract for every managed live-surface Runtime driver."""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import pytest

from mote.contracts.handoff import HandoffRequest, HandoffStatus, HumanHandoffOutcome
from mote.contracts.runtimes import CheckpointFidelity, RuntimeCheckpoint, RuntimeRef
from mote.contracts.surfaces import SurfaceInput, SurfacePresentationMode


@dataclass(frozen=True, slots=True)
class RuntimeDriverConformanceCase:
    """Driver construction plus its one domain-specific state mutation seam."""

    name: str
    driver: Any
    mutate: Callable[[Any], Awaitable[None]]


async def assert_live_runtime_driver_conformance(
    case: RuntimeDriverConformanceCase,
) -> None:
    """Assert lifecycle, handoff fencing and observer-lifetime invariants."""
    driver = case.driver
    assert driver.kind == case.name
    assert driver.capabilities.handoff_modes == frozenset({"exclusive"})
    expected_surface = case.name if case.name != "jupyter" else "notebook"
    assert driver.capabilities.surface_kinds == frozenset({expected_surface})
    assert not (await driver.health()).healthy

    started = await driver.start()
    assert started.restored is False
    assert (await driver.health()).healthy
    with pytest.raises(RuntimeError, match="already started"):
        await driver.start()

    handle = await driver.prepare_handoff(
        HandoffRequest(runtime_ref=RuntimeRef(runtime_id=f"conformance-{case.name}", kind=case.name))
    )
    assert handle.surface.kind in driver.capabilities.surface_kinds
    assert handle.surface.presentation is SurfacePresentationMode.WINDOW
    initial = await driver.snapshot_surface(handle)
    assert initial.media_type
    assert initial.sequence >= 0

    with pytest.raises(RuntimeError, match="already handed off"):
        await driver.prepare_handoff(
            HandoffRequest(runtime_ref=RuntimeRef(runtime_id=f"conformance-{case.name}-second", kind=case.name))
        )

    await driver.finish_handoff(handle, HumanHandoffOutcome(status=HandoffStatus.COMPLETED))
    with pytest.raises(RuntimeError, match="not current"):
        await driver.send_surface_input(handle, SurfaceInput(kind="conformance.invalid", data=""))

    after_sequence = (await driver.snapshot_surface(handle)).sequence
    waiter = asyncio.create_task(driver.next_surface_frame(handle, after_sequence))
    await case.mutate(driver)
    observed = await asyncio.wait_for(waiter, timeout=10)
    assert observed is not None
    assert observed.sequence > after_sequence

    if driver.capabilities.checkpoint_fidelity is CheckpointFidelity.NONE:
        with pytest.raises(RuntimeError):
            await driver.checkpoint("conformance")
    else:
        checkpoint = await driver.checkpoint("conformance")
        assert checkpoint.codec
        assert checkpoint.fidelity is driver.capabilities.checkpoint_fidelity

    await driver.detach_surface(handle)
    with pytest.raises(RuntimeError, match="attachment is not current"):
        await driver.snapshot_surface(handle)

    await driver.aclose()
    assert not (await driver.health()).healthy
    await driver.aclose()


async def assert_handoff_churn_conformance(
    case: RuntimeDriverConformanceCase,
    *,
    cycles: int = 25,
) -> None:
    """Exercise repeated attach/fence/detach cycles without leaking authority."""
    driver = case.driver
    await driver.start()
    try:
        for index in range(cycles):
            handle = await driver.prepare_handoff(
                HandoffRequest(runtime_ref=RuntimeRef(runtime_id=f"churn-{case.name}-{index}", kind=case.name))
            )
            await driver.snapshot_surface(handle)
            await driver.finish_handoff(handle, HumanHandoffOutcome(status=HandoffStatus.COMPLETED))
            with pytest.raises(RuntimeError, match="not current"):
                await driver.send_surface_input(handle, SurfaceInput(kind="conformance.invalid", data=""))
            await driver.detach_surface(handle)
            with pytest.raises(RuntimeError, match="attachment is not current"):
                await driver.snapshot_surface(handle)
            await asyncio.sleep(0)
    finally:
        await driver.aclose()
    assert not (await driver.health()).healthy


async def assert_failed_restore_is_retryable(
    case: RuntimeDriverConformanceCase,
) -> None:
    """A corrupt restore must release partial resources and allow a clean retry."""
    driver = case.driver
    corrupt = RuntimeCheckpoint(
        runtime_id=f"corrupt-{case.name}",
        kind=case.name,
        epoch=1,
        revision=1,
        codec="corrupt-checkpoint@1",
        schema_version=1,
        payload_ref="corrupt:payload",
        fidelity=driver.capabilities.checkpoint_fidelity,
    )
    with pytest.raises((RuntimeError, ValueError)):
        await driver.start(corrupt)
    assert not (await driver.health()).healthy

    started = await driver.start()
    assert started.restored is False
    assert (await driver.health()).healthy
    await driver.aclose()
    assert not (await driver.health()).healthy


__all__ = [
    "RuntimeDriverConformanceCase",
    "assert_failed_restore_is_retryable",
    "assert_handoff_churn_conformance",
    "assert_live_runtime_driver_conformance",
]
