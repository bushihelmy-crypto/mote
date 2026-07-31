"""Fail-closed Shared daemon startup and readiness authority."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from mote.product.inference.backends.sqlite import SQLiteAttemptReceiptStore
from mote.runtime.inference.generation import GatewayGenerationOwner


@dataclass(frozen=True, slots=True)
class SharedStartupResult:
    reconciled_attempts: int
    reconciled_sessions: int
    components: Mapping[str, str]


class SharedDaemonLifecycle:
    def __init__(
        self,
        *,
        persistence: SQLiteAttemptReceiptStore,
        generations: GatewayGenerationOwner,
        hard_min_free_bytes: int,
    ) -> None:
        self._persistence = persistence
        self._generations = generations
        self._hard_min_free_bytes = hard_min_free_bytes
        self._components: dict[str, str] = {
            "persistence": "not_checked",
            "reconciliation": "not_started",
            "generation": "not_active",
            "admission": "closed",
        }

    async def start(self) -> SharedStartupResult:
        await self._persistence.initialize()
        await self._persistence.verify_startup(hard_min_free_bytes=self._hard_min_free_bytes)
        self._components["persistence"] = "ready"
        recovered_generations = await self._persistence.load_generations()
        self._generations.restore(recovered_generations)
        attempts, sessions = await self._persistence.reconcile_incomplete()
        self._components["reconciliation"] = "ready"
        if self._generations.active_generation_id is None:
            self._components["generation"] = "not_active"
            return SharedStartupResult(attempts, sessions, dict(self._components))
        self._components["generation"] = "ready"
        self._components["admission"] = "ready"
        return SharedStartupResult(attempts, sessions, dict(self._components))

    def readiness(self) -> tuple[bool, Mapping[str, str]]:
        ready = all(
            self._components.get(component) == "ready"
            for component in (
                "persistence",
                "reconciliation",
                "generation",
                "admission",
            )
        )
        return ready, dict(self._components)

    def open_admission_after_generation_activation(self) -> None:
        if self._generations.active_generation_id is None:
            raise RuntimeError("cannot open admission without an active generation")
        if self._components["reconciliation"] != "ready":
            raise RuntimeError("cannot open admission before reconciliation")
        self._components["generation"] = "ready"
        self._components["admission"] = "ready"

    def begin_drain(self) -> None:
        self._components["admission"] = "draining"

    def finish_drain(self) -> None:
        self._components["admission"] = "closed"
