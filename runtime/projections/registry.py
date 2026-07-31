"""Versioned Runtime projector registry and durable reconciliation."""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import replace

from mote.contracts.ports.artifact.store import ReliableArtifactPublisher
from mote.contracts.ports.runtime.checkpoint import RuntimeCheckpointPayloadStore
from mote.contracts.ports.runtime.projection import RuntimeProjectionJournal, RuntimeProjector
from mote.contracts.runtime import (
    RuntimeProjectionAck,
    RuntimeProjectionFailure,
    RuntimeProjectionReconcileResult,
    RuntimeProjectionRequest,
)
from mote.runtime.resilience.reconciliation import MAX_RECONCILIATION_ATTEMPTS, is_retryable_reconciliation_error


class RuntimeProjectionRegistry:
    """Resolve exact projector name/schema pairs without fallback guessing."""

    def __init__(self) -> None:
        self._projectors: dict[tuple[str, int], RuntimeProjector] = {}

    def register(self, projector: RuntimeProjector) -> None:
        key = projector.projector, projector.schema_version
        if key in self._projectors:
            raise ValueError(
                "runtime projector is already registered: " f"{projector.projector}@{projector.schema_version}"
            )
        self._projectors[key] = projector

    def resolve(self, request: RuntimeProjectionRequest) -> RuntimeProjector:
        key = request.intent.projector, request.intent.schema_version
        try:
            return self._projectors[key]
        except KeyError as exc:
            raise LookupError(
                "runtime projector is not registered: " f"{request.intent.projector}@{request.intent.schema_version}"
            ) from exc


class RuntimeProjectionReconciler:
    """Materialize, durably publish, then acknowledge independent requests."""

    def __init__(
        self,
        registry: RuntimeProjectionRegistry,
        journal: RuntimeProjectionJournal,
        publisher: ReliableArtifactPublisher,
        checkpoint_payload_store: RuntimeCheckpointPayloadStore | None = None,
    ) -> None:
        self._registry = registry
        self._journal = journal
        self._publisher = publisher
        self._checkpoint_payload_store = checkpoint_payload_store

    async def reconcile(
        self,
        requests: Iterable[RuntimeProjectionRequest],
    ) -> RuntimeProjectionReconcileResult:
        completed = []
        failed = []
        dead_lettered = []
        for request in requests:
            try:
                if self._checkpoint_payload_store is not None:
                    request = replace(
                        request,
                        checkpoint=await self._checkpoint_payload_store.open(request.checkpoint),
                    )
                projector = self._registry.resolve(request)
                publication = await projector.project(request)
                await self._publisher.publish_intent(publication)
                ack = RuntimeProjectionAck(
                    commit_id=request.commit_id,
                    intent_id=request.intent.intent_id,
                )
                await self._journal.acknowledge(ack)
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"[:4096]
                attempts = request.attempts + 1
                retryable = is_retryable_reconciliation_error(exc) and attempts < MAX_RECONCILIATION_ATTEMPTS
                failure = RuntimeProjectionFailure(
                    commit_id=request.commit_id,
                    intent_id=request.intent.intent_id,
                    error=error,
                    retryable=retryable,
                    attempts=attempts,
                )
                await self._journal.acknowledge(
                    RuntimeProjectionAck(
                        commit_id=request.commit_id,
                        intent_id=request.intent.intent_id,
                        status="retry_scheduled" if retryable else "dead_letter",
                        error=error,
                        attempts=attempts,
                    )
                )
                (failed if retryable else dead_lettered).append(failure)
            else:
                completed.append(ack)
        return RuntimeProjectionReconcileResult(
            completed=tuple(completed),
            failed=tuple(failed),
            dead_lettered=tuple(dead_lettered),
        )


__all__ = ["RuntimeProjectionReconciler", "RuntimeProjectionRegistry"]
