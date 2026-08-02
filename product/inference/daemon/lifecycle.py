"""Fail-closed Shared daemon startup and readiness authority."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from mote.product.inference.backends.sqlite import SQLiteAttemptReceiptStore
from mote.runtime.inference.epochs import ExecutionEpochAuthority
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
        epochs: ExecutionEpochAuthority,
        hard_min_free_bytes: int,
    ) -> None:
        self._persistence = persistence
        self._generations = generations
        self._epochs = epochs
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
        self._epochs.replace(await self._persistence.execution_epoch_snapshot())
        self._publish_epochs()
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
        self._epochs.replace(self._persistence_epoch_after_activation())
        self._publish_epochs()

    def _persistence_epoch_after_activation(self):
        current = self._epochs.snapshot()
        return type(current)(current.backup_epoch, current.admission_epoch + 1)

    def _publish_epochs(self) -> None:
        snapshot = self._epochs.snapshot()
        self._components["backup_epoch"] = str(snapshot.backup_epoch)
        self._components["admission_epoch"] = str(snapshot.admission_epoch)

    def refresh_epochs(self) -> None:
        self._publish_epochs()

    def begin_drain(self) -> None:
        self._components["admission"] = "draining"

    def finish_drain(self) -> None:
        self._components["admission"] = "closed"
