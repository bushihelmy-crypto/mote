"""Application-owned Squilla model generations and inference admission."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any

from mote.product.routing.squilla.ml.config import MODEL_BUNDLE_NAME, default_model_dir
from mote.product.routing.squilla.ml.engine import SquillaMLEngine
from mote.product.routing.squilla.ml.inference.types import InferenceRequest, InferenceResult
from mote.runtime.control.lifecycle import LifecyclePhase, LifecycleResource, LifecycleState

ROUTING_MODEL_RESOURCE_NAME = "product-routing-model-runtime"


class RoutingModelActivationError(RuntimeError):
    """A candidate generation failed before the active generation was changed."""


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    return value


@dataclass(frozen=True, slots=True)
class RoutingModelGeneration:
    generation_id: int
    revision: str
    model_dir: Path
    runtime_config: Mapping[str, Any]
    engine: SquillaMLEngine


class RoutingModelLease:
    """One pinned generation, stable for an entire policy decision."""

    __slots__ = ("_generation", "_ml_admitted", "_released", "_runtime")

    def __init__(
        self,
        runtime: "RoutingModelRuntime",
        generation: RoutingModelGeneration,
        *,
        ml_admitted: bool,
    ) -> None:
        self._runtime = runtime
        self._generation = generation
        self._ml_admitted = ml_admitted
        self._released = False

    @property
    def revision(self) -> str:
        return self._generation.revision

    @property
    def model_dir(self) -> Path:
        return self._generation.model_dir

    @property
    def runtime_config(self) -> Mapping[str, Any]:
        return self._generation.runtime_config

    @property
    def ml_admitted(self) -> bool:
        return self._ml_admitted

    @property
    def available(self) -> bool:
        return self._generation.engine.available

    def predict(self, request: InferenceRequest) -> InferenceResult | None:
        if not self._ml_admitted:
            return None
        return self._generation.engine.predict(request)

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        self._runtime._release(self._generation, ml_admitted=self._ml_admitted)

    def __enter__(self) -> "RoutingModelLease":
        return self

    def __exit__(self, _exc_type, _exc, _traceback) -> None:
        self.release()


class RoutingModelRuntime:
    """Own exactly one active model generation for one Product application.

    Candidate bundles are loaded before publication. Decisions pin the active
    generation, so activation cannot mix one generation's prediction with
    another generation's post-processing config. Retired generations drain when
    their final pin is released.
    """

    __slots__ = (
        "_active",
        "_close_task",
        "_condition",
        "_engine_factory",
        "_inference_bulkhead",
        "_next_generation_id",
        "_pins",
        "_retired",
        "_state",
    )

    def __init__(
        self,
        model_dir: str | Path | None = None,
        *,
        revision: str = MODEL_BUNDLE_NAME,
        inference_capacity: int = 1,
        engine_factory: Callable[..., SquillaMLEngine] = SquillaMLEngine,
    ) -> None:
        normalized_revision = revision.strip()
        if not normalized_revision:
            raise ValueError("routing model revision must not be empty")
        if inference_capacity < 1:
            raise ValueError("routing inference capacity must be positive")
        resolved_dir = Path(model_dir) if model_dir is not None else default_model_dir()
        engine = engine_factory(model_dir=resolved_dir)
        self._condition = threading.Condition()
        self._engine_factory = engine_factory
        self._inference_bulkhead = threading.BoundedSemaphore(inference_capacity)
        self._next_generation_id = 2
        self._pins: dict[int, int] = {1: 0}
        self._retired: dict[int, RoutingModelGeneration] = {}
        self._active: RoutingModelGeneration | None = RoutingModelGeneration(
            generation_id=1,
            revision=normalized_revision,
            model_dir=resolved_dir,
            runtime_config=_freeze(engine.config),
            engine=engine,
        )
        self._state = LifecycleState.OPEN
        self._close_task: asyncio.Task[None] | None = None

    @property
    def state(self) -> LifecycleState:
        with self._condition:
            return self._state

    @property
    def active_revision(self) -> str:
        with self._condition:
            if self._active is None:
                raise RuntimeError("routing model runtime is closed")
            return self._active.revision

    @property
    def active_model_dir(self) -> Path:
        with self._condition:
            if self._active is None:
                raise RuntimeError("routing model runtime is closed")
            return self._active.model_dir

    @property
    def draining_revisions(self) -> tuple[str, ...]:
        with self._condition:
            return tuple(generation.revision for generation in self._retired.values())

    def lifecycle_resource(self) -> LifecycleResource:
        return LifecycleResource(
            name=ROUTING_MODEL_RESOURCE_NAME,
            phase=LifecyclePhase.CLOSE_RESOURCES,
            close=self.aclose,
        )

    def pin(self, *, admit_ml: bool = True) -> RoutingModelLease:
        with self._condition:
            if self._state is not LifecycleState.OPEN or self._active is None:
                raise RuntimeError("routing model runtime is closing")
            generation = self._active
            admitted = admit_ml and self._inference_bulkhead.acquire(blocking=False)
            self._pins[generation.generation_id] += 1
        return RoutingModelLease(self, generation, ml_admitted=admitted)

    def _release(
        self,
        generation: RoutingModelGeneration,
        *,
        ml_admitted: bool,
    ) -> None:
        if ml_admitted:
            self._inference_bulkhead.release()
        close_generation = None
        with self._condition:
            generation_id = generation.generation_id
            pins = self._pins[generation_id] - 1
            self._pins[generation_id] = pins
            if pins == 0 and generation_id in self._retired:
                close_generation = self._retired.pop(generation_id)
                self._pins.pop(generation_id)
            self._condition.notify_all()
        if close_generation is not None:
            close_generation.engine.close()

    async def prewarm(self) -> bool:
        """Load the active generation outside the event-loop thread."""

        with self.pin(admit_ml=False) as generation:
            return await asyncio.to_thread(lambda: generation.available)

    async def activate(self, model_dir: str | Path, *, revision: str) -> bool:
        """Prewarm and atomically publish a ready candidate generation.

        Returns ``False`` when the requested immutable identity is already
        active. Any construction or warmup failure leaves the old generation
        active and raises :class:`RoutingModelActivationError`.
        """

        normalized_revision = revision.strip()
        if not normalized_revision:
            raise ValueError("routing model revision must not be empty")
        resolved_dir = Path(model_dir)
        with self._condition:
            if self._state is not LifecycleState.OPEN or self._active is None:
                raise RuntimeError("routing model runtime is closing")
            if self._active.revision == normalized_revision:
                if self._active.model_dir != resolved_dir:
                    raise ValueError(
                        f"routing model revision {normalized_revision!r} already names " f"{self._active.model_dir}"
                    )
                return False
        try:
            candidate_engine = self._engine_factory(model_dir=resolved_dir)
            available = await asyncio.to_thread(lambda: candidate_engine.available)
        except Exception as exc:
            raise RoutingModelActivationError(
                f"routing model generation {normalized_revision!r} failed to prewarm"
            ) from exc
        if not available:
            candidate_engine.close()
            raise RoutingModelActivationError(f"routing model generation {normalized_revision!r} is unavailable")

        candidate = RoutingModelGeneration(
            generation_id=0,
            revision=normalized_revision,
            model_dir=resolved_dir,
            runtime_config=_freeze(candidate_engine.config),
            engine=candidate_engine,
        )
        close_generation = None
        with self._condition:
            if self._state is not LifecycleState.OPEN or self._active is None:
                candidate_engine.close()
                raise RuntimeError("routing model runtime is closing")
            if self._active.revision == normalized_revision:
                candidate_engine.close()
                if self._active.model_dir != resolved_dir:
                    raise ValueError(
                        f"routing model revision {normalized_revision!r} already names " f"{self._active.model_dir}"
                    )
                return False
            candidate = RoutingModelGeneration(
                generation_id=self._next_generation_id,
                revision=candidate.revision,
                model_dir=candidate.model_dir,
                runtime_config=candidate.runtime_config,
                engine=candidate.engine,
            )
            self._next_generation_id += 1
            previous = self._active
            self._active = candidate
            self._pins[candidate.generation_id] = 0
            if self._pins[previous.generation_id] == 0:
                self._pins.pop(previous.generation_id)
                close_generation = previous
            else:
                self._retired[previous.generation_id] = previous
        if close_generation is not None:
            close_generation.engine.close()
        return True

    async def aclose(self) -> None:
        with self._condition:
            if self._state is LifecycleState.CLOSED:
                return
            self._state = LifecycleState.CLOSING
            task = self._close_task
            if task is None or task.cancelled() or (task.done() and task.exception() is not None):
                task = asyncio.create_task(
                    asyncio.to_thread(self._drain_and_close),
                    name="mote-routing-model-close",
                )
                self._close_task = task
        await asyncio.shield(task)

    def _drain_and_close(self) -> None:
        with self._condition:
            while any(self._pins.values()):
                self._condition.wait()
            generations = [*self._retired.values()]
            if self._active is not None:
                generations.append(self._active)
            self._retired.clear()
            self._pins.clear()
            self._active = None
        for generation in generations:
            generation.engine.close()
        with self._condition:
            self._state = LifecycleState.CLOSED


__all__ = [
    "ROUTING_MODEL_RESOURCE_NAME",
    "RoutingModelActivationError",
    "RoutingModelGeneration",
    "RoutingModelLease",
    "RoutingModelRuntime",
]
