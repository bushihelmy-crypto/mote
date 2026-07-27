from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from mote.product.routing.squilla.ml.runtime import RoutingModelActivationError, RoutingModelRuntime
from mote.runtime.lifecycle import LifecycleState


class _Engine:
    def __init__(self, model_dir: str | Path, *, available: bool = True) -> None:
        self.model_dir = Path(model_dir)
        self.config = {"marker": self.model_dir.name}
        self._available = available
        self.closed = False

    @property
    def available(self) -> bool:
        return self._available

    def predict(self, request):
        return request

    def close(self) -> None:
        self.closed = True


def _factory(engines: list[_Engine], *, unavailable: frozenset[str] = frozenset()):
    def build(*, model_dir):
        engine = _Engine(model_dir, available=Path(model_dir).name not in unavailable)
        engines.append(engine)
        return engine

    return build


@pytest.mark.asyncio
async def test_activation_prewarms_then_atomically_swaps() -> None:
    engines: list[_Engine] = []
    runtime = RoutingModelRuntime(
        "/models/old",
        revision="old",
        engine_factory=_factory(engines),
    )

    assert await runtime.activate("/models/new", revision="new") is True

    assert runtime.active_revision == "new"
    assert runtime.active_model_dir == Path("/models/new")
    assert engines[0].closed is True
    assert engines[1].closed is False


@pytest.mark.asyncio
async def test_failed_activation_keeps_previous_generation() -> None:
    engines: list[_Engine] = []
    runtime = RoutingModelRuntime(
        "/models/old",
        revision="old",
        engine_factory=_factory(engines, unavailable=frozenset({"bad"})),
    )

    with pytest.raises(RoutingModelActivationError, match="unavailable"):
        await runtime.activate("/models/bad", revision="bad")

    assert runtime.active_revision == "old"
    assert engines[0].closed is False
    assert engines[1].closed is True


@pytest.mark.asyncio
async def test_pinned_decision_uses_one_generation_until_release() -> None:
    engines: list[_Engine] = []
    runtime = RoutingModelRuntime(
        "/models/old",
        revision="old",
        engine_factory=_factory(engines),
    )
    lease = runtime.pin(admit_ml=False)

    await runtime.activate("/models/new", revision="new")

    assert lease.revision == "old"
    assert lease.runtime_config["marker"] == "old"
    assert runtime.active_revision == "new"
    assert runtime.draining_revisions == ("old",)
    assert engines[0].closed is False

    lease.release()

    assert runtime.draining_revisions == ()
    assert engines[0].closed is True


@pytest.mark.asyncio
async def test_close_rejects_new_work_and_waits_for_pins() -> None:
    engines: list[_Engine] = []
    runtime = RoutingModelRuntime(
        "/models/active",
        revision="active",
        engine_factory=_factory(engines),
    )
    lease = runtime.pin(admit_ml=False)
    close_task = asyncio.create_task(runtime.aclose())
    await asyncio.sleep(0)

    assert runtime.state is LifecycleState.CLOSING
    with pytest.raises(RuntimeError, match="closing"):
        runtime.pin()
    assert close_task.done() is False

    lease.release()
    await close_task

    assert runtime.state is LifecycleState.CLOSED
    assert engines[0].closed is True
