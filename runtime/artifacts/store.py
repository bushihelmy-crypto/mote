"""Durable logical Artifact index over a content-addressed blob store."""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from dataclasses import replace
from pathlib import Path
from threading import RLock
from uuid import uuid4

from mote.contracts.artifacts import (
    ArtifactContentRef,
    ArtifactPublication,
    ArtifactPublicationIntent,
    ArtifactPublicationState,
    ArtifactPublishRequest,
    ArtifactRef,
    ArtifactRepresentationInput,
    ArtifactRetention,
    ArtifactRevision,
    ArtifactSensitivity,
)
from mote.contracts.errors.artifacts import (
    ArtifactIdempotencyConflictError,
    ArtifactNotFoundError,
    ArtifactPublicationTerminalError,
    ArtifactRetentionError,
    ArtifactRevisionConflictError,
)
from mote.contracts.ports.artifact_store import ArtifactBlobStore
from mote.runtime.disk.async_io import run_disk_io

from .ownership import ArtifactOwnership
from .transfer import ArtifactIdempotencyRecord, ArtifactRevisionTransfer

_RETENTION_RANK = {
    ArtifactRetention.EPHEMERAL: 0,
    ArtifactRetention.SESSION: 1,
    ArtifactRetention.PROJECT: 2,
    ArtifactRetention.PINNED: 3,
}
ARTIFACT_INDEX_FILENAME = "artifacts.sqlite3"


class DurableArtifactStore:
    """Revisioned Artifact metadata backed by SQLite and immutable CAS bytes."""

    def __init__(
        self,
        index_path: Path,
        blobs: ArtifactBlobStore,
        *,
        ownership: ArtifactOwnership | None = None,
    ) -> None:
        self._index_path = Path(index_path)
        self._blobs = blobs
        self._ownership = ownership or ArtifactOwnership.standalone()
        self._lock = RLock()

    @property
    def index_path(self) -> Path:
        return self._index_path

    async def publish(self, request: ArtifactPublishRequest) -> ArtifactRevision:
        return await run_disk_io(self._publish, request)

    async def get_revision(
        self,
        artifact_id: str,
        revision: int,
    ) -> ArtifactRevision:
        return await run_disk_io(self._get_revision, artifact_id, revision)

    async def read(self, ref: ArtifactRef) -> bytes:
        return await run_disk_io(self._read, ref)

    async def promote(
        self,
        artifact_id: str,
        revision: int,
        retention: ArtifactRetention,
    ) -> ArtifactRevision:
        return await run_disk_io(
            self._promote,
            artifact_id,
            revision,
            ArtifactRetention(retention),
        )

    async def release(self, artifact_id: str, revision: int) -> bool:
        return await run_disk_io(self._release, artifact_id, revision)

    def release_session_scope(self) -> int:
        """Release expired EPHEMERAL/SESSION metadata during session cleanup."""
        return self.release_retentions((ArtifactRetention.EPHEMERAL, ArtifactRetention.SESSION))

    def release_retentions(self, retentions: tuple[ArtifactRetention, ...]) -> int:
        normalized = tuple(ArtifactRetention(item) for item in retentions)
        values = tuple(item.value for item in normalized)
        if not values:
            return 0
        placeholders = ", ".join("?" for _ in values)
        visible = set(self._ownership.visible_owners())
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                owner_rows = tuple(
                    row
                    for row in connection.execute(
                        "SELECT artifact_id, revision, owner_kind, owner_id "
                        "FROM artifact_owners "
                        f"WHERE released = 0 AND retention IN ({placeholders})",
                        values,
                    ).fetchall()
                    if (row["owner_kind"], row["owner_id"]) in visible
                )
                keys = set()
                for row in owner_rows:
                    keys.add((row["artifact_id"], row["revision"]))
                    self._release_owner(
                        connection,
                        row["artifact_id"],
                        row["revision"],
                        row["owner_kind"],
                        row["owner_id"],
                    )
                publication_ids = tuple(
                    row["publication_id"]
                    for row in connection.execute(
                        "SELECT publication_id, owner_kind, owner_id "
                        "FROM artifact_publication_outbox "
                        "WHERE state IN (?, ?) "
                        f"AND retention IN ({placeholders})",
                        (
                            ArtifactPublicationState.QUEUED.value,
                            ArtifactPublicationState.FAILED.value,
                            *values,
                        ),
                    ).fetchall()
                    if (row["owner_kind"], row["owner_id"]) in visible
                )
                for publication_id in publication_ids:
                    connection.execute(
                        "DELETE FROM artifact_publication_outbox_representations " "WHERE publication_id = ?",
                        (publication_id,),
                    )
                    connection.execute(
                        "DELETE FROM artifact_publication_outbox " "WHERE publication_id = ?",
                        (publication_id,),
                    )
                connection.commit()
                return len(keys)
            except BaseException:
                connection.rollback()
                raise

    async def import_revision(
        self,
        revision: ArtifactRevision,
        retention: ArtifactRetention,
        contents: tuple[bytes, ...],
    ) -> ArtifactRevision:
        return await run_disk_io(
            self._import_revision,
            revision,
            ArtifactRetention(retention),
            contents,
            (),
        )

    def export_revisions(
        self,
        retentions: tuple[ArtifactRetention, ...],
    ) -> tuple[ArtifactRevisionTransfer, ...]:
        """Export live revisions and their idempotency history for scope moves."""
        values = tuple(ArtifactRetention(item).value for item in retentions)
        if not values:
            return ()
        placeholders = ", ".join("?" for _ in values)
        with self._lock, self._connect() as connection:
            keys = tuple(
                (row["artifact_id"], row["revision"])
                for row in connection.execute(
                    "SELECT DISTINCT artifact_id, revision "
                    "FROM artifact_representations "
                    f"WHERE released = 0 AND retention IN ({placeholders}) "
                    "ORDER BY artifact_id, revision",
                    values,
                ).fetchall()
            )
            revisions = tuple(self._load_revision(connection, artifact_id, revision) for artifact_id, revision in keys)
            records = {
                key: tuple(
                    ArtifactIdempotencyRecord(
                        idempotency_key=row["idempotency_key"],
                        artifact_id=row["artifact_id"],
                        revision=row["revision"],
                        request_fingerprint=row["request_fingerprint"],
                    )
                    for row in connection.execute(
                        "SELECT idempotency_key, artifact_id, revision, "
                        "request_fingerprint FROM artifact_publications "
                        "WHERE artifact_id = ? AND revision = ? "
                        "ORDER BY idempotency_key",
                        key,
                    ).fetchall()
                )
                for key in keys
            }
        exports = []
        for revision in revisions:
            contents = tuple(
                self._blobs.read_bytes(
                    ArtifactContentRef(
                        content_ref=ref.content_ref,
                        digest=ref.digest,
                        size=ref.size,
                    )
                )
                for ref in revision.representations
            )
            exports.append(
                ArtifactRevisionTransfer(
                    revision=revision,
                    contents=contents,
                    idempotency_records=records[(revision.artifact_id, revision.revision)],
                )
            )
        return tuple(exports)

    def export_transfer(
        self,
        artifact_id: str,
        revision: int,
    ) -> ArtifactRevisionTransfer:
        """Export one exact live revision for a scoped promotion."""
        with self._lock, self._connect() as connection:
            exported = self._load_revision(connection, artifact_id, revision)
            records = tuple(
                ArtifactIdempotencyRecord(
                    idempotency_key=row["idempotency_key"],
                    artifact_id=row["artifact_id"],
                    revision=row["revision"],
                    request_fingerprint=row["request_fingerprint"],
                )
                for row in connection.execute(
                    "SELECT idempotency_key, artifact_id, revision, "
                    "request_fingerprint FROM artifact_publications "
                    "WHERE artifact_id = ? AND revision = ? "
                    "ORDER BY idempotency_key",
                    (artifact_id, revision),
                ).fetchall()
            )
        contents = tuple(
            self._blobs.read_bytes(
                ArtifactContentRef(
                    content_ref=ref.content_ref,
                    digest=ref.digest,
                    size=ref.size,
                )
            )
            for ref in exported.representations
        )
        return ArtifactRevisionTransfer(exported, contents, records)

    def has_idempotency_key(self, idempotency_key: str) -> bool:
        if type(idempotency_key) is not str or not idempotency_key:
            return False
        with self._lock, self._connect() as connection:
            return (
                connection.execute(
                    "SELECT 1 FROM artifact_publications " "WHERE idempotency_key = ? LIMIT 1",
                    (idempotency_key,),
                ).fetchone()
                is not None
            )

    def import_transfer(
        self,
        transfer: ArtifactRevisionTransfer,
        retention: ArtifactRetention,
    ) -> ArtifactRevision:
        if not isinstance(transfer, ArtifactRevisionTransfer):
            raise TypeError("artifact import requires a revision transfer")
        return self._import_revision(
            transfer.revision,
            ArtifactRetention(retention),
            transfer.contents,
            transfer.idempotency_records,
        )

    def release_transfer(self, transfer: ArtifactRevisionTransfer) -> bool:
        """Release an exact exported revision after destination verification."""
        if not isinstance(transfer, ArtifactRevisionTransfer):
            raise TypeError("artifact release requires a revision transfer")
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                current = self._load_revision(
                    connection,
                    transfer.revision.artifact_id,
                    transfer.revision.revision,
                )
            except ArtifactNotFoundError:
                connection.commit()
                return False
            if current != transfer.revision:
                raise ArtifactIdempotencyConflictError(
                    "artifact revision changed during scope migration",
                    artifact_id=transfer.revision.artifact_id,
                    revision=transfer.revision.revision,
                )
            self._release_revision(
                connection,
                transfer.revision.artifact_id,
                transfer.revision.revision,
            )
            connection.commit()
            return True

    def _import_revision(
        self,
        revision: ArtifactRevision,
        retention: ArtifactRetention,
        contents: tuple[bytes, ...],
        idempotency_records: tuple[ArtifactIdempotencyRecord, ...],
    ) -> ArtifactRevision:
        if len(contents) != len(revision.representations):
            raise ValueError("artifact import content arity does not match revision")
        prepared = tuple(self._blobs.put_bytes(content) for content in contents)
        for source, content in zip(revision.representations, prepared, strict=True):
            if source.digest != content.digest or source.size != content.size:
                raise ValueError("artifact import content does not match its source")
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT 1 FROM artifact_representations "
                "WHERE artifact_id = ? AND revision = ? AND released = 0 LIMIT 1",
                (revision.artifact_id, revision.revision),
            ).fetchone()
            if existing is not None:
                imported = self._load_revision(
                    connection,
                    revision.artifact_id,
                    revision.revision,
                    require_visible=False,
                )
                expected = tuple(
                    replace(
                        source,
                        content_ref=content.content_ref,
                        retention=retention,
                    )
                    for source, content in zip(revision.representations, prepared, strict=True)
                )
                normalized_imported = tuple(replace(item, retention=retention) for item in imported.representations)
                if normalized_imported != expected:
                    raise ArtifactIdempotencyConflictError(
                        "artifact revision conflicts with an existing scope import",
                        artifact_id=revision.artifact_id,
                        revision=revision.revision,
                    )
            else:
                refs = []
                for source, content in zip(revision.representations, prepared, strict=True):
                    ref = replace(
                        source,
                        content_ref=content.content_ref,
                        digest=content.digest,
                        size=content.size,
                        retention=retention,
                    )
                    connection.execute(
                        "INSERT INTO artifact_representations "
                        "(artifact_id, revision, representation, kind, mime_type, "
                        "content_ref, digest, size, retention, sensitivity, suggested_name) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        self._row_values(ref),
                    )
                    refs.append(ref)
                imported = ArtifactRevision(
                    artifact_id=revision.artifact_id,
                    revision=revision.revision,
                    representations=tuple(refs),
                )
            self._add_owner(
                connection,
                revision.artifact_id,
                revision.revision,
                retention,
            )
            for record in idempotency_records:
                existing_record = connection.execute(
                    "SELECT artifact_id, revision, request_fingerprint "
                    "FROM artifact_publications WHERE idempotency_key = ?",
                    (record.idempotency_key,),
                ).fetchone()
                expected_record = (
                    record.artifact_id,
                    record.revision,
                    record.request_fingerprint,
                )
                if existing_record is None:
                    connection.execute(
                        "INSERT INTO artifact_publications "
                        "(idempotency_key, artifact_id, revision, request_fingerprint) "
                        "VALUES (?, ?, ?, ?)",
                        (record.idempotency_key, *expected_record),
                    )
                elif tuple(existing_record) != expected_record:
                    raise ArtifactIdempotencyConflictError(
                        "artifact idempotency history conflicts during scope import",
                        idempotency_key=record.idempotency_key,
                    )
            connection.commit()
            return self._load_revision(
                connection,
                revision.artifact_id,
                revision.revision,
            )

    def scan_content_roots(self) -> tuple[ArtifactContentRef, ...]:
        """Return every CAS object still referenced by durable Artifact metadata.

        Both committed representations and staged outbox representations are
        roots.  The latter matters during crash recovery: publication bytes are
        materialized before the logical revision is committed and must remain
        readable while the outbox is queued or failed.
        """
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                "SELECT content_ref, digest, size FROM artifact_representations "
                "WHERE released = 0 "
                "UNION SELECT content_ref, digest, size "
                "FROM artifact_publication_outbox_representations AS representations "
                "JOIN artifact_publication_outbox AS outbox "
                "ON outbox.publication_id = representations.publication_id "
                "WHERE outbox.state IN ('queued', 'failed')"
            ).fetchall()
        roots: dict[str, ArtifactContentRef] = {}
        for row in rows:
            ref = ArtifactContentRef(
                content_ref=row["content_ref"],
                digest=row["digest"],
                size=row["size"],
            )
            prior = roots.get(ref.digest)
            if prior is not None and prior.size != ref.size:
                raise ValueError("artifact index digest resolves to conflicting content sizes")
            roots[ref.digest] = ref
        return tuple(sorted(roots.values(), key=lambda item: (item.digest, item.size)))

    async def stage(
        self,
        publication_id: str,
        request: ArtifactPublishRequest,
    ) -> ArtifactPublication:
        return await run_disk_io(self._stage, publication_id, request)

    async def stage_intent(
        self,
        intent: ArtifactPublicationIntent,
    ) -> ArtifactPublication:
        return await run_disk_io(self._stage_intent, intent)

    async def pending_ids(self, limit: int = 100) -> tuple[str, ...]:
        return await run_disk_io(self._pending_ids, limit)

    async def load(self, publication_id: str) -> ArtifactPublication:
        return await run_disk_io(self._load_staged, publication_id)

    async def acknowledge(
        self,
        publication_id: str,
        revision: ArtifactRevision,
    ) -> None:
        await run_disk_io(self._acknowledge, publication_id, revision)

    async def record_failure(self, publication_id: str, error: str) -> None:
        await run_disk_io(self._record_failure, publication_id, error)

    async def dead_letter(self, publication_id: str, error: str) -> None:
        await run_disk_io(self._dead_letter, publication_id, error)

    def export_pending_publications(
        self,
        retentions: tuple[ArtifactRetention, ...],
    ) -> tuple[ArtifactPublication, ...]:
        """Export recoverable outbox work for a retention-scope migration."""
        values = tuple(ArtifactRetention(item).value for item in retentions)
        if not values:
            return ()
        placeholders = ", ".join("?" for _ in values)
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM artifact_publication_outbox "
                f"WHERE retention IN ({placeholders}) AND state IN (?, ?) "
                "ORDER BY publication_id",
                (
                    *values,
                    ArtifactPublicationState.QUEUED.value,
                    ArtifactPublicationState.FAILED.value,
                ),
            ).fetchall()
            return tuple(self._load_publication(connection, row) for row in rows)

    def import_publication(self, publication: ArtifactPublication) -> ArtifactPublication:
        """Idempotently move recoverable outbox work into this physical tier."""
        if not isinstance(publication, ArtifactPublication):
            raise TypeError("artifact publication import requires a publication")
        self._stage(publication.publication_id, publication.request)
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM artifact_publication_outbox WHERE publication_id = ?",
                (publication.publication_id,),
            ).fetchone()
            existing = self._load_publication(connection, row)
            if existing.request != publication.request:
                raise ArtifactIdempotencyConflictError(
                    "artifact publication conflicts during scope import",
                    publication_id=publication.publication_id,
                )
            if existing == publication:
                connection.commit()
                return existing
            destination_is_newer = (
                existing.state
                in {
                    ArtifactPublicationState.COMPLETED,
                    ArtifactPublicationState.DEAD_LETTER,
                }
                or existing.attempts >= publication.attempts
                and existing.state is not ArtifactPublicationState.QUEUED
            )
            if destination_is_newer:
                connection.commit()
                return existing
            connection.execute(
                "UPDATE artifact_publication_outbox SET state = ?, attempts = ?, "
                "last_error = ?, result_artifact_id = ?, result_revision = ? "
                "WHERE publication_id = ?",
                (
                    publication.state.value,
                    publication.attempts,
                    publication.last_error,
                    publication.result_artifact_id,
                    publication.result_revision,
                    publication.publication_id,
                ),
            )
            row = connection.execute(
                "SELECT * FROM artifact_publication_outbox WHERE publication_id = ?",
                (publication.publication_id,),
            ).fetchone()
            imported = self._load_publication(connection, row)
            connection.commit()
            return imported

    def discard_publication(self, publication: ArtifactPublication) -> bool:
        """Remove one exact source outbox item after its destination is durable."""
        if not isinstance(publication, ArtifactPublication):
            raise TypeError("artifact publication discard requires a publication")
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM artifact_publication_outbox WHERE publication_id = ?",
                (publication.publication_id,),
            ).fetchone()
            if row is None:
                connection.commit()
                return False
            current = self._load_publication(connection, row)
            if current != publication:
                raise ArtifactIdempotencyConflictError(
                    "artifact publication changed during scope migration",
                    publication_id=publication.publication_id,
                )
            connection.execute(
                "DELETE FROM artifact_publication_outbox_representations " "WHERE publication_id = ?",
                (publication.publication_id,),
            )
            connection.execute(
                "DELETE FROM artifact_publication_outbox WHERE publication_id = ?",
                (publication.publication_id,),
            )
            connection.commit()
            return True

    def _stage(
        self,
        publication_id: str,
        request: ArtifactPublishRequest,
    ) -> ArtifactPublication:
        self._validate_publication_id(publication_id)
        self._assert_not_dead_letter(publication_id)
        effective_request = replace(
            request,
            idempotency_key=request.idempotency_key or self._default_idempotency_key(publication_id),
        )
        prepared = tuple(
            (representation, self._blobs.put_bytes(representation.content))
            for representation in effective_request.representations
        )
        return self._stage_prepared(publication_id, effective_request, prepared)

    def _stage_intent(
        self,
        intent: ArtifactPublicationIntent,
    ) -> ArtifactPublication:
        self._validate_publication_id(intent.publication_id)
        self._assert_not_dead_letter(intent.publication_id)
        request = ArtifactPublishRequest(
            artifact_id=intent.artifact_id,
            expected_revision=intent.expected_revision,
            retention=intent.retention,
            sensitivity=intent.sensitivity,
            idempotency_key=intent.idempotency_key,
            representations=tuple(
                ArtifactRepresentationInput(
                    representation=item.representation,
                    kind=item.kind,
                    mime_type=item.mime_type,
                    content=b"",
                    suggested_name=item.suggested_name,
                )
                for item in intent.representations
            ),
        )
        effective_request = replace(
            request,
            idempotency_key=request.idempotency_key or self._default_idempotency_key(intent.publication_id),
        )
        prepared = tuple(
            (representation, materialized.content)
            for representation, materialized in zip(
                effective_request.representations,
                intent.representations,
                strict=True,
            )
        )
        for _representation, content_ref in prepared:
            content = self._blobs.read_bytes(content_ref)
            if len(content) != content_ref.size or hashlib.sha256(content).hexdigest() != content_ref.digest:
                raise ValueError("artifact CAS content does not match its reference")
        return self._stage_prepared(
            intent.publication_id,
            effective_request,
            prepared,
        )

    def _stage_prepared(
        self,
        publication_id: str,
        effective_request: ArtifactPublishRequest,
        prepared: tuple,
    ) -> ArtifactPublication:
        fingerprint = self._outbox_fingerprint(effective_request, prepared)
        owner_kind, owner_id = self._ownership.owner_for(effective_request.retention)
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                existing = connection.execute(
                    "SELECT * FROM artifact_publication_outbox " "WHERE publication_id = ?",
                    (publication_id,),
                ).fetchone()
                if existing is not None:
                    if existing["state"] == ArtifactPublicationState.DEAD_LETTER.value:
                        raise ArtifactPublicationTerminalError(
                            "artifact publication is permanently dead-lettered",
                            publication_id=publication_id,
                            last_error=existing["last_error"],
                        )
                    if existing["request_fingerprint"] != fingerprint:
                        raise ArtifactIdempotencyConflictError(
                            "artifact publication id was reused with a different request",
                            publication_id=publication_id,
                        )
                    publication = self._load_publication(connection, existing)
                    connection.commit()
                    return publication
                connection.execute(
                    "INSERT INTO artifact_publication_outbox "
                    "(publication_id, artifact_id, expected_revision, retention, "
                    "sensitivity, idempotency_key, request_fingerprint, state, "
                    "attempts, last_error, result_artifact_id, result_revision, "
                    "owner_kind, owner_id) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, '', '', NULL, ?, ?)",
                    (
                        publication_id,
                        effective_request.artifact_id,
                        effective_request.expected_revision,
                        effective_request.retention.value,
                        effective_request.sensitivity.value,
                        effective_request.idempotency_key,
                        fingerprint,
                        ArtifactPublicationState.QUEUED.value,
                        owner_kind,
                        owner_id,
                    ),
                )
                for representation, content in prepared:
                    connection.execute(
                        "INSERT INTO artifact_publication_outbox_representations "
                        "(publication_id, representation, kind, mime_type, content_ref, "
                        "digest, size, suggested_name) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            publication_id,
                            representation.representation,
                            representation.kind,
                            representation.mime_type,
                            content.content_ref,
                            content.digest,
                            content.size,
                            representation.suggested_name,
                        ),
                    )
                row = connection.execute(
                    "SELECT * FROM artifact_publication_outbox " "WHERE publication_id = ?",
                    (publication_id,),
                ).fetchone()
                publication = self._load_publication(connection, row)
                connection.commit()
                return publication
            except BaseException:
                connection.rollback()
                raise

    def _pending_ids(self, limit: int) -> tuple[str, ...]:
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("artifact publication pending limit must be 1..1000")
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                "SELECT publication_id FROM artifact_publication_outbox "
                "WHERE state IN (?, ?) "
                "AND ((owner_kind = ? AND owner_id = ?) "
                "OR (owner_kind = ? AND owner_id = ?) "
                "OR (owner_kind = ? AND owner_id = ?)) "
                "ORDER BY (state = ?) DESC, rowid LIMIT ?",
                (
                    ArtifactPublicationState.QUEUED.value,
                    ArtifactPublicationState.FAILED.value,
                    *self._ownership.visible_owners()[0],
                    *self._ownership.visible_owners()[1],
                    *self._ownership.visible_owners()[2],
                    ArtifactPublicationState.QUEUED.value,
                    limit,
                ),
            ).fetchall()
            return tuple(row["publication_id"] for row in rows)

    def _assert_not_dead_letter(self, publication_id: str) -> None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT state, last_error FROM artifact_publication_outbox " "WHERE publication_id = ?",
                (publication_id,),
            ).fetchone()
        if row is not None and row["state"] == ArtifactPublicationState.DEAD_LETTER.value:
            raise ArtifactPublicationTerminalError(
                "artifact publication is permanently dead-lettered",
                publication_id=publication_id,
                last_error=row["last_error"],
            )

    def _load_staged(self, publication_id: str) -> ArtifactPublication:
        self._validate_publication_id(publication_id)
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM artifact_publication_outbox WHERE publication_id = ?",
                (publication_id,),
            ).fetchone()
            if row is None:
                raise ArtifactNotFoundError(
                    "artifact publication does not exist",
                    publication_id=publication_id,
                )
            return self._load_publication(connection, row)

    def _acknowledge(
        self,
        publication_id: str,
        revision: ArtifactRevision,
    ) -> None:
        self._validate_publication_id(publication_id)
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    "SELECT * FROM artifact_publication_outbox " "WHERE publication_id = ?",
                    (publication_id,),
                ).fetchone()
                if row is None:
                    raise ArtifactNotFoundError(
                        "artifact publication does not exist",
                        publication_id=publication_id,
                    )
                stored_revision = self._load_revision(
                    connection,
                    revision.artifact_id,
                    revision.revision,
                )
                if stored_revision != revision:
                    raise ArtifactIdempotencyConflictError(
                        "artifact publication acknowledgement is not the durable revision",
                        publication_id=publication_id,
                    )
                publication_record = connection.execute(
                    "SELECT artifact_id, revision, request_fingerprint "
                    "FROM artifact_publications WHERE idempotency_key = ?",
                    (row["idempotency_key"],),
                ).fetchone()
                staged_request, prepared = self._outbox_metadata(connection, row)
                expected_fingerprint = self._request_fingerprint(
                    staged_request,
                    prepared,
                )
                if (
                    publication_record is None
                    or publication_record["artifact_id"] != revision.artifact_id
                    or publication_record["revision"] != revision.revision
                    or publication_record["request_fingerprint"] != expected_fingerprint
                    or row["request_fingerprint"] != self._outbox_fingerprint(staged_request, prepared)
                ):
                    raise ArtifactIdempotencyConflictError(
                        "artifact publication result does not match the staged request",
                        publication_id=publication_id,
                    )
                if row["state"] == ArtifactPublicationState.COMPLETED.value:
                    if row["result_artifact_id"] != revision.artifact_id or row["result_revision"] != revision.revision:
                        raise ArtifactIdempotencyConflictError(
                            "artifact publication was acknowledged with a different result",
                            publication_id=publication_id,
                        )
                    connection.commit()
                    return
                connection.execute(
                    "UPDATE artifact_publication_outbox SET state = ?, "
                    "attempts = attempts + 1, last_error = '', result_artifact_id = ?, "
                    "result_revision = ? WHERE publication_id = ?",
                    (
                        ArtifactPublicationState.COMPLETED.value,
                        revision.artifact_id,
                        revision.revision,
                        publication_id,
                    ),
                )
                connection.commit()
            except BaseException:
                connection.rollback()
                raise

    def _record_failure(self, publication_id: str, error: str) -> None:
        self._validate_publication_id(publication_id)
        if type(error) is not str:
            raise TypeError("artifact publication failure must be a string")
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    "SELECT state FROM artifact_publication_outbox " "WHERE publication_id = ?",
                    (publication_id,),
                ).fetchone()
                if row is None:
                    raise ArtifactNotFoundError(
                        "artifact publication does not exist",
                        publication_id=publication_id,
                    )
                if row["state"] not in {
                    ArtifactPublicationState.COMPLETED.value,
                    ArtifactPublicationState.DEAD_LETTER.value,
                }:
                    connection.execute(
                        "UPDATE artifact_publication_outbox SET state = ?, "
                        "attempts = attempts + 1, last_error = ? "
                        "WHERE publication_id = ?",
                        (
                            ArtifactPublicationState.FAILED.value,
                            error[:4096],
                            publication_id,
                        ),
                    )
                connection.commit()
            except BaseException:
                connection.rollback()
                raise

    def _dead_letter(self, publication_id: str, error: str) -> None:
        self._validate_publication_id(publication_id)
        if type(error) is not str:
            raise TypeError("artifact publication failure must be a string")
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                "UPDATE artifact_publication_outbox SET state = ?, "
                "attempts = attempts + 1, last_error = ? "
                "WHERE publication_id = ? AND state NOT IN (?, ?)",
                (
                    ArtifactPublicationState.DEAD_LETTER.value,
                    error[:4096],
                    publication_id,
                    ArtifactPublicationState.COMPLETED.value,
                    ArtifactPublicationState.DEAD_LETTER.value,
                ),
            )
            if cursor.rowcount == 0:
                row = connection.execute(
                    "SELECT 1 FROM artifact_publication_outbox WHERE publication_id = ?",
                    (publication_id,),
                ).fetchone()
                if row is None:
                    raise ArtifactNotFoundError(
                        "artifact publication does not exist",
                        publication_id=publication_id,
                    )

    def _publish(self, request: ArtifactPublishRequest) -> ArtifactRevision:
        prepared = tuple(
            (representation, self._blobs.put_bytes(representation.content))
            for representation in request.representations
        )
        artifact_id = request.artifact_id or uuid4().hex
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                if request.idempotency_key:
                    existing = connection.execute(
                        "SELECT artifact_id, revision, request_fingerprint "
                        "FROM artifact_publications "
                        "WHERE idempotency_key = ?",
                        (request.idempotency_key,),
                    ).fetchone()
                    if existing is not None:
                        revision = self._load_revision(
                            connection,
                            existing["artifact_id"],
                            existing["revision"],
                            require_visible=False,
                        )
                        fingerprint = self._request_fingerprint(request, prepared)
                        recorded_fingerprint = existing["request_fingerprint"]
                        if recorded_fingerprint:
                            matches = recorded_fingerprint == fingerprint
                        else:
                            matches = self._legacy_publication_matches(
                                request,
                                prepared,
                                revision,
                            )
                            if matches:
                                connection.execute(
                                    "UPDATE artifact_publications "
                                    "SET request_fingerprint = ? "
                                    "WHERE idempotency_key = ?",
                                    (fingerprint, request.idempotency_key),
                                )
                        if not matches:
                            raise ArtifactIdempotencyConflictError(
                                "artifact idempotency key was reused with different content",
                                idempotency_key=request.idempotency_key,
                            )
                        if not self._visible_owner_rows(
                            connection,
                            revision.artifact_id,
                            revision.revision,
                        ):
                            self._add_owner(
                                connection,
                                revision.artifact_id,
                                revision.revision,
                                request.retention,
                            )
                        connection.commit()
                        return self._load_revision(
                            connection,
                            revision.artifact_id,
                            revision.revision,
                        )

                row = connection.execute(
                    "SELECT MAX(revision) AS revision FROM artifact_representations " "WHERE artifact_id = ?",
                    (artifact_id,),
                ).fetchone()
                current = int(row["revision"] or 0)
                if current and request.expected_revision is None:
                    raise ArtifactRevisionConflictError(
                        "publishing a new artifact revision requires expected_revision",
                        artifact_id=artifact_id,
                        current_revision=current,
                    )
                expected = request.expected_revision
                if expected is not None and expected != current:
                    raise ArtifactRevisionConflictError(
                        "artifact revision changed",
                        artifact_id=artifact_id,
                        expected_revision=expected,
                        current_revision=current,
                    )
                revision_number = current + 1
                refs = []
                for representation, content in prepared:
                    ref = ArtifactRef(
                        artifact_id=artifact_id,
                        revision=revision_number,
                        representation=representation.representation,
                        kind=representation.kind,
                        mime_type=representation.mime_type,
                        content_ref=content.content_ref,
                        digest=content.digest,
                        size=content.size,
                        retention=request.retention,
                        sensitivity=request.sensitivity,
                        suggested_name=representation.suggested_name,
                    )
                    connection.execute(
                        "INSERT INTO artifact_representations "
                        "(artifact_id, revision, representation, kind, mime_type, "
                        "content_ref, digest, size, retention, sensitivity, suggested_name) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        self._row_values(ref),
                    )
                    refs.append(ref)
                self._add_owner(
                    connection,
                    artifact_id,
                    revision_number,
                    request.retention,
                )
                if request.idempotency_key:
                    connection.execute(
                        "INSERT INTO artifact_publications "
                        "(idempotency_key, artifact_id, revision, request_fingerprint) "
                        "VALUES (?, ?, ?, ?)",
                        (
                            request.idempotency_key,
                            artifact_id,
                            revision_number,
                            self._request_fingerprint(request, prepared),
                        ),
                    )
                connection.commit()
                return ArtifactRevision(
                    artifact_id=artifact_id,
                    revision=revision_number,
                    representations=tuple(refs),
                )
            except BaseException:
                connection.rollback()
                raise

    def _get_revision(self, artifact_id: str, revision: int) -> ArtifactRevision:
        with self._lock, self._connect() as connection:
            return self._load_revision(connection, artifact_id, revision)

    def _read(self, ref: ArtifactRef) -> bytes:
        with self._lock, self._connect() as connection:
            revision = self._load_revision(
                connection,
                ref.artifact_id,
                ref.revision,
            )
            try:
                stored = revision.get(ref.representation)
            except KeyError:
                raise ArtifactNotFoundError(
                    "artifact representation does not exist",
                    artifact_id=ref.artifact_id,
                    revision=ref.revision,
                    representation=ref.representation,
                ) from None
            if replace(stored, retention=ref.retention) != ref:
                raise ArtifactNotFoundError(
                    "artifact reference does not match the durable index",
                    artifact_id=ref.artifact_id,
                    revision=ref.revision,
                    representation=ref.representation,
                )
        return self._blobs.read_bytes(
            ArtifactContentRef(
                content_ref=stored.content_ref,
                digest=stored.digest,
                size=stored.size,
            )
        )

    def _promote(
        self,
        artifact_id: str,
        revision: int,
        retention: ArtifactRetention,
    ) -> ArtifactRevision:
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                self._load_revision(connection, artifact_id, revision)
                owner_rows = self._visible_owner_rows(
                    connection,
                    artifact_id,
                    revision,
                )
                source_owner = min(
                    owner_rows,
                    key=lambda row: _RETENTION_RANK[ArtifactRetention(row["retention"])],
                )
                current_retention = ArtifactRetention(source_owner["retention"])
                if _RETENTION_RANK[retention] < _RETENTION_RANK[current_retention]:
                    raise ArtifactRetentionError(
                        "artifact retention cannot be demoted",
                        artifact_id=artifact_id,
                        revision=revision,
                        current_retention=current_retention.value,
                        requested_retention=retention.value,
                    )
                target_kind, target_id = self._ownership.owner_for(retention)
                self._add_owner(connection, artifact_id, revision, retention)
                if (source_owner["owner_kind"], source_owner["owner_id"]) != (
                    target_kind,
                    target_id,
                ):
                    connection.execute(
                        "UPDATE artifact_owners SET released = 1 "
                        "WHERE artifact_id = ? AND revision = ? "
                        "AND owner_kind = ? AND owner_id = ?",
                        (
                            artifact_id,
                            revision,
                            source_owner["owner_kind"],
                            source_owner["owner_id"],
                        ),
                    )
                self._sync_revision_retention(connection, artifact_id, revision)
                connection.commit()
                return self._load_revision(connection, artifact_id, revision)
            except BaseException:
                connection.rollback()
                raise

    def _release(self, artifact_id: str, revision: int) -> bool:
        if type(artifact_id) is not str or not artifact_id:
            raise ValueError("artifact_id must be non-empty")
        if type(revision) is not int or revision < 1:
            raise ValueError("artifact revision must be positive")
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                owners = self._visible_owner_rows(connection, artifact_id, revision)
                if not owners:
                    connection.commit()
                    return False
                self._release_revision(connection, artifact_id, revision)
                connection.commit()
                return True
            except BaseException:
                connection.rollback()
                raise

    def _release_revision(
        self,
        connection: sqlite3.Connection,
        artifact_id: str,
        revision: int,
    ) -> None:
        for row in self._visible_owner_rows(connection, artifact_id, revision):
            self._release_owner(
                connection,
                artifact_id,
                revision,
                row["owner_kind"],
                row["owner_id"],
            )

    @staticmethod
    def _release_owner(
        connection: sqlite3.Connection,
        artifact_id: str,
        revision: int,
        owner_kind: str,
        owner_id: str,
    ) -> None:
        connection.execute(
            "UPDATE artifact_owners SET released = 1 "
            "WHERE artifact_id = ? AND revision = ? "
            "AND owner_kind = ? AND owner_id = ?",
            (artifact_id, revision, owner_kind, owner_id),
        )
        DurableArtifactStore._finalize_unowned_revision(
            connection,
            artifact_id,
            revision,
        )

    @staticmethod
    def _finalize_unowned_revision(
        connection: sqlite3.Connection,
        artifact_id: str,
        revision: int,
    ) -> None:
        owner = connection.execute(
            "SELECT 1 FROM artifact_owners " "WHERE artifact_id = ? AND revision = ? AND released = 0 LIMIT 1",
            (artifact_id, revision),
        ).fetchone()
        if owner is not None:
            DurableArtifactStore._sync_revision_retention(
                connection,
                artifact_id,
                revision,
            )
            return
        completed_ids = tuple(
            row["publication_id"]
            for row in connection.execute(
                "SELECT publication_id FROM artifact_publication_outbox "
                "WHERE state = ? AND result_artifact_id = ? AND result_revision = ?",
                (
                    ArtifactPublicationState.COMPLETED.value,
                    artifact_id,
                    revision,
                ),
            ).fetchall()
        )
        for publication_id in completed_ids:
            connection.execute(
                "DELETE FROM artifact_publication_outbox_representations " "WHERE publication_id = ?",
                (publication_id,),
            )
            connection.execute(
                "DELETE FROM artifact_publication_outbox WHERE publication_id = ?",
                (publication_id,),
            )
        connection.execute(
            "DELETE FROM artifact_publications " "WHERE artifact_id = ? AND revision = ?",
            (artifact_id, revision),
        )
        connection.execute(
            "UPDATE artifact_representations SET released = 1 " "WHERE artifact_id = ? AND revision = ?",
            (artifact_id, revision),
        )

    def _connect(self) -> sqlite3.Connection:
        self._index_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if os.name == "posix":
            os.chmod(self._index_path.parent, 0o700)
        connection = sqlite3.connect(self._index_path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA synchronous = FULL")
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS artifact_representations (
                artifact_id TEXT NOT NULL,
                revision INTEGER NOT NULL,
                representation TEXT NOT NULL,
                kind TEXT NOT NULL,
                mime_type TEXT NOT NULL,
                content_ref TEXT NOT NULL,
                digest TEXT NOT NULL,
                size INTEGER NOT NULL,
                retention TEXT NOT NULL,
                sensitivity TEXT NOT NULL,
                suggested_name TEXT NOT NULL,
                released INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (artifact_id, revision, representation)
            );
            CREATE TABLE IF NOT EXISTS artifact_publications (
                idempotency_key TEXT PRIMARY KEY,
                artifact_id TEXT NOT NULL,
                revision INTEGER NOT NULL,
                request_fingerprint TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS artifact_owners (
                artifact_id TEXT NOT NULL,
                revision INTEGER NOT NULL,
                owner_kind TEXT NOT NULL CHECK (
                    owner_kind IN ("session", "project", "global")
                ),
                owner_id TEXT NOT NULL,
                retention TEXT NOT NULL,
                released INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (artifact_id, revision, owner_kind, owner_id)
            );
            CREATE TABLE IF NOT EXISTS artifact_publication_outbox (
                publication_id TEXT PRIMARY KEY,
                artifact_id TEXT NOT NULL,
                expected_revision INTEGER,
                retention TEXT NOT NULL,
                sensitivity TEXT NOT NULL,
                idempotency_key TEXT NOT NULL,
                request_fingerprint TEXT NOT NULL,
                state TEXT NOT NULL,
                attempts INTEGER NOT NULL,
                last_error TEXT NOT NULL,
                result_artifact_id TEXT NOT NULL,
                result_revision INTEGER,
                owner_kind TEXT NOT NULL DEFAULT "session",
                owner_id TEXT NOT NULL DEFAULT "standalone"
            );
            CREATE TABLE IF NOT EXISTS artifact_publication_outbox_representations (
                publication_id TEXT NOT NULL,
                representation TEXT NOT NULL,
                kind TEXT NOT NULL,
                mime_type TEXT NOT NULL,
                content_ref TEXT NOT NULL,
                digest TEXT NOT NULL,
                size INTEGER NOT NULL,
                suggested_name TEXT NOT NULL,
                PRIMARY KEY (publication_id, representation)
            );
            """
        )
        publication_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(artifact_publications)").fetchall()
        }
        if "request_fingerprint" not in publication_columns:
            connection.execute(
                "ALTER TABLE artifact_publications " "ADD COLUMN request_fingerprint TEXT NOT NULL DEFAULT ''"
            )
        representation_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(artifact_representations)").fetchall()
        }
        if "released" not in representation_columns:
            connection.execute("ALTER TABLE artifact_representations " "ADD COLUMN released INTEGER NOT NULL DEFAULT 0")
        outbox_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(artifact_publication_outbox)").fetchall()
        }
        if "owner_kind" not in outbox_columns:
            connection.execute(
                "ALTER TABLE artifact_publication_outbox " 'ADD COLUMN owner_kind TEXT NOT NULL DEFAULT "session"'
            )
        if "owner_id" not in outbox_columns:
            connection.execute(
                "ALTER TABLE artifact_publication_outbox " 'ADD COLUMN owner_id TEXT NOT NULL DEFAULT "standalone"'
            )
        self._backfill_ownership(connection)
        connection.commit()
        if os.name == "posix":
            os.chmod(self._index_path, 0o600)
        return connection

    def _backfill_ownership(self, connection: sqlite3.Connection) -> None:
        """Give pre-ownership rows one exact owner without changing identity."""
        rows = connection.execute(
            "SELECT artifact_id, revision, retention, MIN(released) AS released "
            "FROM artifact_representations GROUP BY artifact_id, revision, retention"
        ).fetchall()
        for row in rows:
            retention = ArtifactRetention(row["retention"])
            owner_kind, owner_id = self._ownership.owner_for(retention)
            owner_count = connection.execute(
                "SELECT COUNT(*) FROM artifact_owners " "WHERE artifact_id = ? AND revision = ?",
                (row["artifact_id"], row["revision"]),
            ).fetchone()[0]
            if owner_count == 0:
                connection.execute(
                    "INSERT INTO artifact_owners "
                    "(artifact_id, revision, owner_kind, owner_id, retention, released) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        row["artifact_id"],
                        row["revision"],
                        owner_kind,
                        owner_id,
                        retention.value,
                        row["released"],
                    ),
                )
                continue
            if owner_id == "standalone":
                continue
            standalone = connection.execute(
                "SELECT retention, released FROM artifact_owners "
                "WHERE artifact_id = ? AND revision = ? "
                "AND owner_kind = ? AND owner_id = 'standalone'",
                (row["artifact_id"], row["revision"], owner_kind),
            ).fetchone()
            if standalone is None:
                continue
            connection.execute(
                "INSERT OR IGNORE INTO artifact_owners "
                "(artifact_id, revision, owner_kind, owner_id, retention, released) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    row["artifact_id"],
                    row["revision"],
                    owner_kind,
                    owner_id,
                    standalone["retention"],
                    standalone["released"],
                ),
            )
            connection.execute(
                "DELETE FROM artifact_owners "
                "WHERE artifact_id = ? AND revision = ? "
                "AND owner_kind = ? AND owner_id = 'standalone'",
                (row["artifact_id"], row["revision"], owner_kind),
            )
        outbox_rows = connection.execute(
            "SELECT publication_id, retention, owner_kind, owner_id " "FROM artifact_publication_outbox"
        ).fetchall()
        for row in outbox_rows:
            if row["owner_id"] != "standalone":
                continue
            owner_kind, owner_id = self._ownership.owner_for(ArtifactRetention(row["retention"]))
            connection.execute(
                "UPDATE artifact_publication_outbox SET owner_kind = ?, owner_id = ? " "WHERE publication_id = ?",
                (owner_kind, owner_id, row["publication_id"]),
            )

    @staticmethod
    def _row_values(ref: ArtifactRef) -> tuple:
        return (
            ref.artifact_id,
            ref.revision,
            ref.representation,
            ref.kind,
            ref.mime_type,
            ref.content_ref,
            ref.digest,
            ref.size,
            ref.retention.value,
            ref.sensitivity.value,
            ref.suggested_name,
        )

    def _visible_owner_rows(
        self,
        connection: sqlite3.Connection,
        artifact_id: str,
        revision: int,
    ) -> tuple[sqlite3.Row, ...]:
        visible = set(self._ownership.visible_owners())
        return tuple(
            row
            for row in connection.execute(
                "SELECT owner_kind, owner_id, retention FROM artifact_owners "
                "WHERE artifact_id = ? AND revision = ? AND released = 0",
                (artifact_id, revision),
            ).fetchall()
            if (row["owner_kind"], row["owner_id"]) in visible
        )

    @staticmethod
    def _effective_retention(owner_rows: tuple[sqlite3.Row, ...]) -> ArtifactRetention:
        return max(
            (ArtifactRetention(row["retention"]) for row in owner_rows),
            key=_RETENTION_RANK.__getitem__,
        )

    def _add_owner(
        self,
        connection: sqlite3.Connection,
        artifact_id: str,
        revision: int,
        retention: ArtifactRetention,
    ) -> None:
        retention = ArtifactRetention(retention)
        owner_kind, owner_id = self._ownership.owner_for(retention)
        connection.execute(
            "INSERT INTO artifact_owners "
            "(artifact_id, revision, owner_kind, owner_id, retention, released) "
            "VALUES (?, ?, ?, ?, ?, 0) "
            "ON CONFLICT(artifact_id, revision, owner_kind, owner_id) DO UPDATE SET "
            "retention = excluded.retention, released = 0",
            (artifact_id, revision, owner_kind, owner_id, retention.value),
        )
        self._sync_revision_retention(connection, artifact_id, revision)

    @staticmethod
    def _sync_revision_retention(
        connection: sqlite3.Connection,
        artifact_id: str,
        revision: int,
    ) -> None:
        rows = connection.execute(
            "SELECT retention FROM artifact_owners " "WHERE artifact_id = ? AND revision = ? AND released = 0",
            (artifact_id, revision),
        ).fetchall()
        if not rows:
            connection.execute(
                "UPDATE artifact_representations SET released = 1 " "WHERE artifact_id = ? AND revision = ?",
                (artifact_id, revision),
            )
            return
        retention = max(
            (ArtifactRetention(row["retention"]) for row in rows),
            key=_RETENTION_RANK.__getitem__,
        )
        connection.execute(
            "UPDATE artifact_representations SET retention = ?, released = 0 " "WHERE artifact_id = ? AND revision = ?",
            (retention.value, artifact_id, revision),
        )

    def _load_revision(
        self,
        connection: sqlite3.Connection,
        artifact_id: str,
        revision: int,
        *,
        require_visible: bool = True,
    ) -> ArtifactRevision:
        rows = connection.execute(
            "SELECT * FROM artifact_representations "
            "WHERE artifact_id = ? AND revision = ? AND released = 0 "
            "ORDER BY representation",
            (artifact_id, revision),
        ).fetchall()
        if not rows:
            raise ArtifactNotFoundError(
                "artifact revision does not exist",
                artifact_id=artifact_id,
                revision=revision,
            )
        owner_rows = self._visible_owner_rows(connection, artifact_id, revision)
        if require_visible and not owner_rows:
            raise ArtifactNotFoundError(
                "artifact revision is not owned by this session or project",
                artifact_id=artifact_id,
                revision=revision,
            )
        if not owner_rows:
            owner_rows = tuple(
                connection.execute(
                    "SELECT owner_kind, owner_id, retention FROM artifact_owners "
                    "WHERE artifact_id = ? AND revision = ? AND released = 0",
                    (artifact_id, revision),
                ).fetchall()
            )
        retention = self._effective_retention(owner_rows)
        refs = tuple(
            ArtifactRef(
                artifact_id=row["artifact_id"],
                revision=row["revision"],
                representation=row["representation"],
                kind=row["kind"],
                mime_type=row["mime_type"],
                content_ref=row["content_ref"],
                digest=row["digest"],
                size=row["size"],
                retention=retention,
                sensitivity=ArtifactSensitivity(row["sensitivity"]),
                suggested_name=row["suggested_name"],
            )
            for row in rows
        )
        return ArtifactRevision(
            artifact_id=artifact_id,
            revision=revision,
            representations=refs,
        )

    @staticmethod
    def _request_fingerprint(
        request: ArtifactPublishRequest,
        prepared: tuple,
    ) -> str:
        payload = {
            "artifact_id": request.artifact_id,
            "expected_revision": request.expected_revision,
            "representations": sorted(
                (
                    representation.representation,
                    representation.kind,
                    representation.mime_type,
                    content.digest,
                    content.size,
                    representation.suggested_name,
                )
                for representation, content in prepared
            ),
            "retention": request.retention.value,
            "sensitivity": request.sensitivity.value,
        }
        canonical = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        return hashlib.sha256(canonical).hexdigest()

    @classmethod
    def _outbox_fingerprint(
        cls,
        request: ArtifactPublishRequest,
        prepared: tuple,
    ) -> str:
        canonical = json.dumps(
            {
                "request": cls._request_fingerprint(request, prepared),
                "idempotency_key": request.idempotency_key,
            },
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        return hashlib.sha256(canonical).hexdigest()

    def _load_publication(
        self,
        connection: sqlite3.Connection,
        row: sqlite3.Row,
    ) -> ArtifactPublication:
        representation_rows = connection.execute(
            "SELECT * FROM artifact_publication_outbox_representations "
            "WHERE publication_id = ? ORDER BY representation",
            (row["publication_id"],),
        ).fetchall()
        representations = tuple(
            ArtifactRepresentationInput(
                representation=item["representation"],
                kind=item["kind"],
                mime_type=item["mime_type"],
                content=self._blobs.read_bytes(
                    ArtifactContentRef(
                        content_ref=item["content_ref"],
                        digest=item["digest"],
                        size=item["size"],
                    )
                ),
                suggested_name=item["suggested_name"],
            )
            for item in representation_rows
        )
        return ArtifactPublication(
            publication_id=row["publication_id"],
            request=ArtifactPublishRequest(
                artifact_id=row["artifact_id"],
                expected_revision=row["expected_revision"],
                retention=ArtifactRetention(row["retention"]),
                sensitivity=ArtifactSensitivity(row["sensitivity"]),
                idempotency_key=row["idempotency_key"],
                representations=representations,
            ),
            state=ArtifactPublicationState(row["state"]),
            attempts=row["attempts"],
            last_error=row["last_error"],
            result_artifact_id=row["result_artifact_id"],
            result_revision=row["result_revision"],
        )

    @staticmethod
    def _outbox_metadata(
        connection: sqlite3.Connection,
        row: sqlite3.Row,
    ) -> tuple[ArtifactPublishRequest, tuple]:
        representation_rows = connection.execute(
            "SELECT * FROM artifact_publication_outbox_representations "
            "WHERE publication_id = ? ORDER BY representation",
            (row["publication_id"],),
        ).fetchall()
        representations = tuple(
            ArtifactRepresentationInput(
                representation=item["representation"],
                kind=item["kind"],
                mime_type=item["mime_type"],
                content=b"",
                suggested_name=item["suggested_name"],
            )
            for item in representation_rows
        )
        request = ArtifactPublishRequest(
            artifact_id=row["artifact_id"],
            expected_revision=row["expected_revision"],
            retention=ArtifactRetention(row["retention"]),
            sensitivity=ArtifactSensitivity(row["sensitivity"]),
            idempotency_key=row["idempotency_key"],
            representations=representations,
        )
        prepared = tuple(
            (
                representation,
                ArtifactContentRef(
                    content_ref=item["content_ref"],
                    digest=item["digest"],
                    size=item["size"],
                ),
            )
            for representation, item in zip(
                representations,
                representation_rows,
                strict=True,
            )
        )
        return request, prepared

    @staticmethod
    def _validate_publication_id(publication_id: str) -> None:
        if (
            type(publication_id) is not str
            or not publication_id
            or len(publication_id) > 256
            or any(ord(character) < 32 for character in publication_id)
        ):
            raise ValueError("artifact publication_id is invalid")

    @staticmethod
    def _default_idempotency_key(publication_id: str) -> str:
        digest = hashlib.sha256(publication_id.encode("utf-8")).hexdigest()
        return f"artifact-publication:{digest}"

    @staticmethod
    def _legacy_publication_matches(
        request: ArtifactPublishRequest,
        prepared: tuple,
        existing: ArtifactRevision,
    ) -> bool:
        if request.artifact_id and request.artifact_id != existing.artifact_id:
            return False
        actual = {
            item.representation: (
                item.kind,
                item.mime_type,
                item.digest,
                item.size,
                item.suggested_name,
            )
            for item in existing.representations
        }
        expected = {
            representation.representation: (
                representation.kind,
                representation.mime_type,
                content.digest,
                content.size,
                representation.suggested_name,
            )
            for representation, content in prepared
        }
        return (
            actual == expected
            and all(
                _RETENTION_RANK[item.retention] >= _RETENTION_RANK[request.retention]
                for item in existing.representations
            )
            and all(item.sensitivity is request.sensitivity for item in existing.representations)
        )


__all__ = ["ARTIFACT_INDEX_FILENAME", "DurableArtifactStore"]
