"""Reliable publication of staged Artifacts."""
from __future__ import annotations

from mote.contracts.artifacts import (
    ArtifactPublication,
    ArtifactPublicationFailure,
    ArtifactPublicationIntent,
    ArtifactPublicationReconcileResult,
    ArtifactPublicationResult,
    ArtifactPublishRequest,
    ArtifactRevision,
)
from mote.contracts.ports import ArtifactPublicationOutbox, ArtifactStore
from mote.runtime.reconciliation import MAX_RECONCILIATION_ATTEMPTS, is_retryable_reconciliation_error


class ReliableArtifactPublisher:
    """At-least-once publisher over a durable Artifact publication outbox.

    Staging is the local durability boundary. A crash after publication but
    before acknowledgement replays the same request; its stable idempotency key
    makes that replay resolve to the original revision.

    This does not make an upstream Runtime commit and outbox staging one atomic
    transaction. Callers whose commit is durably recorded elsewhere must retain
    enough deterministic state to reconstruct a missing publication intent
    after a crash in that pre-stage window.
    """

    def __init__(
        self,
        outbox: ArtifactPublicationOutbox,
        store: ArtifactStore,
    ) -> None:
        self._outbox = outbox
        self._store = store

    async def publish(
        self,
        publication_id: str,
        request: ArtifactPublishRequest,
    ) -> ArtifactRevision:
        publication = await self._outbox.stage(publication_id, request)
        try:
            return await self._publish(publication)
        except Exception as exc:
            await self._outbox.record_failure(
                publication.publication_id,
                self._error_text(exc),
            )
            raise

    async def publish_intent(
        self,
        intent: ArtifactPublicationIntent,
    ) -> ArtifactRevision:
        publication = await self._outbox.stage_intent(intent)
        try:
            return await self._publish(publication)
        except Exception as exc:
            await self._outbox.record_failure(
                publication.publication_id,
                self._error_text(exc),
            )
            raise

    async def reconcile_pending(
        self,
        limit: int = 100,
    ) -> ArtifactPublicationReconcileResult:
        published = []
        failed = []
        dead_lettered = []
        for publication_id in await self._outbox.pending_ids(limit):
            publication = None
            try:
                publication = await self._outbox.load(publication_id)
                revision = await self._publish(publication)
            except Exception as exc:
                error = self._error_text(exc)
                attempts = (publication.attempts if publication is not None else 0) + 1
                retryable = is_retryable_reconciliation_error(exc) and attempts < MAX_RECONCILIATION_ATTEMPTS
                failure = ArtifactPublicationFailure(
                    publication_id=publication_id,
                    error=error,
                    retryable=retryable,
                    attempts=attempts,
                )
                if retryable:
                    await self._outbox.record_failure(publication_id, error)
                    failed.append(failure)
                else:
                    await self._outbox.dead_letter(publication_id, error)
                    dead_lettered.append(failure)
            else:
                published.append(
                    ArtifactPublicationResult(
                        publication_id=publication.publication_id,
                        revision=revision,
                    )
                )
        return ArtifactPublicationReconcileResult(
            published=tuple(published),
            failed=tuple(failed),
            dead_lettered=tuple(dead_lettered),
        )

    async def _publish(self, publication: ArtifactPublication) -> ArtifactRevision:
        revision = await self._store.publish(publication.request)
        await self._outbox.acknowledge(publication.publication_id, revision)
        return revision

    @staticmethod
    def _error_text(exc: Exception) -> str:
        return f"{type(exc).__name__}: {exc}"[:4096]


__all__ = ["ReliableArtifactPublisher"]
