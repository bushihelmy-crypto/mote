from __future__ import annotations

import pytest

from mote.contracts.artifacts import (
    ArtifactPublicationState,
    ArtifactPublishRequest,
    ArtifactRepresentationInput,
    ArtifactRetention,
)
from mote.contracts.errors.artifacts import ArtifactNotFoundError
from mote.runtime.artifacts import (
    ArtifactRepositoryBlobStore,
    ArtifactRepositoryLayout,
    DurableArtifactStore,
    LegacyArtifactMigrator,
)
from mote.runtime.fileops.artifact_budgets import ARTIFACT_HARD_LIMIT_BYTES
from mote.runtime.fileops.artifact_repository import ArtifactRepository
from mote.runtime.session.events import SessionMetaEvent
from mote.runtime.session.log import SessionLog


def _request(
    content: bytes,
    *,
    artifact_id: str,
    retention: ArtifactRetention,
    idempotency_key: str = "",
) -> ArtifactPublishRequest:
    return ArtifactPublishRequest(
        artifact_id=artifact_id,
        retention=retention,
        idempotency_key=idempotency_key,
        representations=(
            ArtifactRepresentationInput(
                representation="text",
                kind="report",
                mime_type="text/plain",
                content=content,
                suggested_name="report.txt",
            ),
        ),
    )


def _legacy_store(tmp_path, session_id: str, project_root: str) -> DurableArtifactStore:
    session_dir = tmp_path / ".agent_sessions" / session_id
    log = SessionLog(session_id, base_dir=str(session_dir.parent))
    log.commit_offline(
        SessionMetaEvent(
            session_id=session_id,
            working_dir=project_root,
            project_root=project_root,
        )
    )
    repository = ArtifactRepository(
        session_dir / "blobs",
        hard_limit_bytes=ARTIFACT_HARD_LIMIT_BYTES,
    )
    return DurableArtifactStore(
        session_dir / "artifacts.sqlite3",
        ArtifactRepositoryBlobStore(repository),
    )


@pytest.mark.asyncio
async def test_migrates_project_pinned_and_pending_outbox_losslessly(tmp_path):
    project_root = str(tmp_path / "project")
    source = _legacy_store(tmp_path, "legacy", project_root)
    project_request = _request(
        b"project",
        artifact_id="project-report",
        retention=ArtifactRetention.PROJECT,
        idempotency_key="project-once",
    )
    project = await source.publish(project_request)
    pinned = await source.publish(
        _request(
            b"pinned",
            artifact_id="pinned-report",
            retention=ArtifactRetention.PINNED,
        )
    )
    publication_request = _request(
        b"queued",
        artifact_id="queued-report",
        retention=ArtifactRetention.PROJECT,
    )
    publication = await source.stage("legacy-publication", publication_request)
    await source.record_failure(publication.publication_id, "temporary")

    report = LegacyArtifactMigrator(tmp_path).migrate()

    assert report.migrated_revisions == 2
    assert report.migrated_publications == 1
    assert report.failures == ()
    layout = ArtifactRepositoryLayout(tmp_path)
    store = layout.open(layout.ownership(session_id="new-session", project_root=project_root)).store
    assert await store.read(project.get("text")) == b"project"
    assert await store.read(pinned.get("text")) == b"pinned"
    migrated_publication = await store.load("legacy-publication")
    assert migrated_publication.state is ArtifactPublicationState.FAILED
    assert migrated_publication.attempts == 1
    assert migrated_publication.last_error == "temporary"
    replayed = await store.publish(project_request)
    assert replayed.artifact_id == project.artifact_id
    assert replayed.revision == project.revision
    with pytest.raises(ArtifactNotFoundError):
        await source.get_revision(project.artifact_id, project.revision)
    with pytest.raises(ArtifactNotFoundError):
        await source.load("legacy-publication")

    repeated = LegacyArtifactMigrator(tmp_path).migrate()
    assert repeated.migrated_revisions == 0
    assert repeated.migrated_publications == 0
    assert repeated.skipped_sources == 1


@pytest.mark.asyncio
async def test_rerun_finishes_crash_after_destination_import(tmp_path):
    project_root = str(tmp_path / "project")
    source = _legacy_store(tmp_path, "legacy", project_root)
    revision = await source.publish(
        _request(
            b"survives",
            artifact_id="crash-safe",
            retention=ArtifactRetention.PROJECT,
        )
    )
    transfer = source.export_revisions((ArtifactRetention.PROJECT,))[0]
    layout = ArtifactRepositoryLayout(tmp_path)
    destination = layout.open(layout.ownership(session_id="new-session", project_root=project_root)).store
    destination.import_transfer(transfer, ArtifactRetention.PROJECT)

    report = LegacyArtifactMigrator(tmp_path).migrate()

    assert report.migrated_revisions == 1
    assert report.failures == ()
    assert await destination.read(revision.get("text")) == b"survives"
    with pytest.raises(ArtifactNotFoundError):
        await source.get_revision(revision.artifact_id, revision.revision)


@pytest.mark.asyncio
async def test_missing_project_identity_fails_closed_and_preserves_source(tmp_path):
    source = _legacy_store(tmp_path, "legacy", str(tmp_path / "project"))
    revision = await source.publish(
        _request(
            b"keep me",
            artifact_id="unowned",
            retention=ArtifactRetention.PROJECT,
        )
    )
    (tmp_path / ".agent_sessions" / "legacy" / "rollout.jsonl").write_text(
        "not-json\n",
        encoding="utf-8",
    )

    report = LegacyArtifactMigrator(tmp_path).migrate()

    assert len(report.failures) == 1
    assert "authoritative project root" in report.failures[0]
    assert await source.read(revision.get("text")) == b"keep me"


@pytest.mark.asyncio
async def test_destination_identity_conflict_preserves_both_authorities(tmp_path):
    project_root = str(tmp_path / "project")
    source = _legacy_store(tmp_path, "legacy", project_root)
    source_revision = await source.publish(
        _request(
            b"source",
            artifact_id="same-id",
            retention=ArtifactRetention.PROJECT,
        )
    )
    layout = ArtifactRepositoryLayout(tmp_path)
    destination = layout.open(layout.ownership(session_id="new-session", project_root=project_root)).store
    destination_revision = await destination.publish(
        _request(
            b"destination",
            artifact_id="same-id",
            retention=ArtifactRetention.PROJECT,
        )
    )

    report = LegacyArtifactMigrator(tmp_path).migrate()

    assert len(report.failures) == 1
    assert "conflicts with an existing scope import" in report.failures[0]
    assert await source.read(source_revision.get("text")) == b"source"
    assert await destination.read(destination_revision.get("text")) == b"destination"
