from __future__ import annotations

import asyncio

import pytest

from mote.runtime.lifecycle import LifecycleCloseError, LifecycleStack, LifecycleState


@pytest.mark.asyncio
async def test_phase_failure_closes_siblings_and_blocks_downstream_until_retry() -> None:
    events: list[str] = []
    attempts = 0

    async def retrying() -> None:
        nonlocal attempts
        attempts += 1
        events.append(f"retry:{attempts}")
        if attempts == 1:
            raise RuntimeError("transient")

    stack = LifecycleStack()
    stack.register_close("downstream", lambda: events.append("downstream"), phase=200)
    stack.register_close("sibling", lambda: events.append("sibling"), phase=100)
    stack.register_close("retrying", retrying, phase=100)

    with pytest.raises(LifecycleCloseError) as caught:
        await stack.aclose()
    assert caught.value.phase == 100
    assert [failure.name for failure in caught.value.failures] == ["retrying"]
    assert events == ["retry:1", "sibling"]
    assert stack.resource_names == ("downstream", "retrying")

    await stack.aclose()
    assert events == ["retry:1", "sibling", "retry:2", "downstream"]
    assert stack.state is LifecycleState.CLOSED


@pytest.mark.asyncio
async def test_waiter_cancellation_does_not_cancel_shutdown() -> None:
    entered = asyncio.Event()
    release = asyncio.Event()

    async def close() -> None:
        entered.set()
        await release.wait()

    stack = LifecycleStack()
    stack.register_close("slow", close, phase=100)
    waiter = asyncio.create_task(stack.aclose())
    await entered.wait()
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    release.set()
    await stack.aclose()
    assert stack.state is LifecycleState.CLOSED
