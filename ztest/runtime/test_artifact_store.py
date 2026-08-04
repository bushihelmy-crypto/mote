from __future__ import annotations

import asyncio
import hashlib
import sqlite3
from contextlib import nullcontext

import pytest

from mote.contracts.artifact import (
    ArtifactContentRef,
    ArtifactHold,
    ArtifactHoldKind,
    ArtifactPublishRequest,
    ArtifactRef,
    ArtifactRepresentationInput,
    ArtifactRetention,
)
from mote.contracts.artifact.errors import (
    ArtifactIdempotencyConflictError,
    ArtifactNotFoundError,
    ArtifactRetentionError,
    ArtifactRevisionConflictError,
)
from mote.contracts.content import ContentIdentity
from mote.contracts.ports.artifact.store import ArtifactBlobStore, ArtifactStore
from mote.runtime.artifacts import (
    ArtifactGarbageCollector,
    ArtifactOwnership,
    ContentAddressedArtifactBlobStore,
    DurableArtifactStore,
)
from mote.runtime.artifacts.repository import ContentAddressedArtifactStore
from mote.runtime.control.leases import InMemoryLeaseCoordinator


class MemoryBlobs:
    def __init__(self) -> None:
        self.contents = {}
        self.put_calls = 0

    def put_bytes(self, content: bytes) -> ArtifactContentRef:
        self.put_calls += 1
        digest = hashlib.sha256(content).hexdigest()
        self.contents[digest] = content
        return ArtifactContentRef(
            identity=ContentIdentity(digest, len(content)),
            locator=f"sha256:{digest}",
        )

    def read_bytes(self, ref: ArtifactContentRef) -> bytes:
        content = self.contents[ref.identity.digest]
        assert len(content) == ref.identity.size
        assert hashlib.sha256(content).hexdigest() == ref.identity.digest
        return content


def _request(
    content: bytes,
    *,
    artifact_id: str = "",
    expected_revision: int | None = None,
    idempotency_key: str = "",
    retention: ArtifactRetention = ArtifactRetention.SESSION,
) -> ArtifactPublishRequest:
    return ArtifactPublishRequest(
        artifact_id=artifact_id,
        expected_revision=expected_revision,
        idempotency_key=idempotency_key,
        retention=retention,
        representations=(
            ArtifactRepresentationInput(
                representation="svg",
                kind="canvas",
                mime_type="image/svg+xml",
                content=content,
                suggested_name="diagram.svg",
            ),
        ),
    )


def test_artifact_store_satisfies_public_protocols(tmp_path):
    blobs = MemoryBlobs()
    store = DurableArtifactStore(tmp_path / "artifacts.sqlite3", blobs)

    assert isinstance(blobs, ArtifactBlobStore)
    assert isinstance(store, ArtifactStore)


@pytest.mark.asyncio
async def test_publish_read_and_reopen_durable_revision(tmp_path):
    blobs = MemoryBlobs()
    path = tmp_path / "artifacts.sqlite3"
    store = DurableArtifactStore(path, blobs)

    published = await store.publish(_request(b"<svg>one</svg>"))
    ref = published.get("svg")

    assert published.revision == 1
    assert ref.readable.startswith("artifact:")
    assert await store.read(ref) == b"<svg>one</svg>"

    reopened = DurableArtifactStore(path, blobs)
    assert await reopened.get_revision(published.artifact_id, 1) == published


@pytest.mark.asyncio
async def test_artifact_io_does_not_depend_on_default_asyncio_executor(
    tmp_path,
    monkeypatch,
):
    async def reject_default_executor(*args, **kwargs):
        raise AssertionError("Artifact I/O must use the dedicated disk boundary")

    monkeypatch.setattr(asyncio, "to_thread", reject_default_executor)
    blobs = MemoryBlobs()
    store = DurableArtifactStore(tmp_path / "artifacts.sqlite3", blobs)
    request = _request(b"<svg>dedicated-io</svg>")

    publication = await store.stage("publication-dedicated-io", request)
    revision = await store.publish(publication.request)
    await store.acknowledge(publication.publication_id, revision)

    assert await store.read(revision.get("svg")) == b"<svg>dedicated-io</svg>"


@pytest.mark.asyncio
async def test_read_rejects_forged_reference_to_shared_blob(tmp_path):
    blobs = MemoryBlobs()
    store = DurableArtifactStore(tmp_path / "artifacts.sqlite3", blobs)
    published = await store.publish(_request(b"owned by the artifact"))
    stored = published.get("svg")
    unrelated = blobs.put_bytes(b"unrelated file snapshot")
    forged = ArtifactRef(
        artifact_id=stored.artifact_id,
        revision=stored.revision,
        representation=stored.representation,
        kind=stored.kind,
        mime_type=stored.mime_type,
        content_ref=unrelated.content_ref,
        digest=unrelated.identity.digest,
        size=unrelated.identity.size,
        retention=stored.retention,
        sensitivity=stored.sensitivity,
        suggested_name=stored.suggested_name,
    )

    with pytest.raises(ArtifactNotFoundError):
        await store.read(forged)


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("artifact_id", "../escape"),
        ("artifact_id", "nested/path"),
        ("representation", "../svg"),
        ("representation", "nested/svg"),
        ("suggested_name", "../diagram.svg"),
        ("suggested_name", "nested/diagram.svg"),
        ("suggested_name", "nested\\diagram.svg"),
    ),
)
def test_artifact_publication_rejects_path_bearing_identifiers(field, value):
    representation_args = {
        "representation": "svg",
        "kind": "canvas",
        "mime_type": "image/svg+xml",
        "content": b"<svg/>",
        "suggested_name": "diagram.svg",
    }
    request_args = {}
    if field == "artifact_id":
        request_args[field] = value
    else:
        representation_args[field] = value

    with pytest.raises(ValueError):
        ArtifactPublishRequest(
            representations=(ArtifactRepresentationInput(**representation_args),),
            **request_args,
        )


@pytest.mark.parametrize(
    "content_ref",
    (
        "/tmp/artifact",
        "../artifact",
        "nested/artifact",
        "C:\\Users\\user\\artifact",
        "file:///tmp/artifact",
    ),
)
def test_artifact_content_reference_never_exposes_a_filesystem_path(content_ref):
    with pytest.raises(ValueError):
        ArtifactContentRef(
            identity=ContentIdentity("0" * 64, 0),
            locator=content_ref,
        )


@pytest.mark.asyncio
async def test_revision_requires_optimistic_expected_revision(tmp_path):
    store = DurableArtifactStore(tmp_path / "artifacts.sqlite3", MemoryBlobs())
    first = await store.publish(_request(b"one", artifact_id="canvas"))

    with pytest.raises(ArtifactRevisionConflictError):
        await store.publish(_request(b"two", artifact_id="canvas"))
    with pytest.raises(ArtifactRevisionConflictError):
        await store.publish(_request(b"two", artifact_id="canvas", expected_revision=9))

    second = await store.publish(
        _request(
            b"two",
            artifact_id="canvas",
            expected_revision=first.revision,
        )
    )
    assert second.revision == 2


@pytest.mark.asyncio
async def test_idempotency_replays_same_revision_and_rejects_different_content(
    tmp_path,
):
    store = DurableArtifactStore(tmp_path / "artifacts.sqlite3", MemoryBlobs())
    request = _request(b"same", idempotency_key="tool-call-1")

    first = await store.publish(request)
    replayed = await store.publish(request)

    assert replayed == first
    with pytest.raises(ArtifactIdempotencyConflictError):
        await store.publish(_request(b"different", idempotency_key="tool-call-1"))


@pytest.mark.asyncio
async def test_idempotent_replay_is_stable_after_retention_promotion(tmp_path):
    store = DurableArtifactStore(tmp_path / "artifacts.sqlite3", MemoryBlobs())
    request = _request(
        b"same",
        idempotency_key="tool-call-1",
        retention=ArtifactRetention.EPHEMERAL,
    )
    first = await store.publish(request)
    await store.promote(
        first.artifact_id,
        first.revision,
        ArtifactRetention.PINNED,
    )

    replayed = await store.publish(request)

    assert replayed.artifact_id == first.artifact_id
    assert replayed.revision == first.revision
    assert replayed.get("svg").retention is ArtifactRetention.PINNED


@pytest.mark.asyncio
async def test_retention_is_monotonic(tmp_path):
    store = DurableArtifactStore(tmp_path / "artifacts.sqlite3", MemoryBlobs())
    published = await store.publish(_request(b"content", retention=ArtifactRetention.EPHEMERAL))

    promoted = await store.promote(
        published.artifact_id,
        published.revision,
        ArtifactRetention.PINNED,
    )
    assert promoted.get("svg").retention is ArtifactRetention.PINNED

    with pytest.raises(ArtifactRetentionError):
        await store.promote(
            published.artifact_id,
            published.revision,
            ArtifactRetention.SESSION,
        )


@pytest.mark.asyncio
async def test_release_unroots_revision_without_reusing_its_identity(tmp_path):
    store = DurableArtifactStore(tmp_path / "artifacts.sqlite3", MemoryBlobs())
    first = await store.publish(_request(b"first", artifact_id="canvas"))

    assert await store.release("canvas", first.revision) is True
    assert await store.release("canvas", first.revision) is False
    assert store.scan_content_roots() == ()
    with pytest.raises(ArtifactNotFoundError):
        await store.get_revision("canvas", first.revision)

    second = await store.publish(
        _request(
            b"second",
            artifact_id="canvas",
            expected_revision=first.revision,
        )
    )
    assert second.revision == 2


@pytest.mark.asyncio
async def test_concurrent_publishers_are_revision_fenced(tmp_path):
    store = DurableArtifactStore(tmp_path / "artifacts.sqlite3", MemoryBlobs())
    first = await store.publish(_request(b"one", artifact_id="canvas"))

    outcomes = await asyncio.gather(
        store.publish(
            _request(
                b"two-a",
                artifact_id="canvas",
                expected_revision=first.revision,
            )
        ),
        store.publish(
            _request(
                b"two-b",
                artifact_id="canvas",
                expected_revision=first.revision,
            )
        ),
        return_exceptions=True,
    )

    assert sum(not isinstance(item, BaseException) for item in outcomes) == 1
    assert sum(isinstance(item, ArtifactRevisionConflictError) for item in outcomes) == 1


@pytest.mark.asyncio
async def test_project_and_pinned_scopes_are_resolved_across_sessions(tmp_path):
    blobs = MemoryBlobs()
    index = tmp_path / "artifacts.sqlite3"

    def scoped(session_name: str, project_name: str) -> DurableArtifactStore:
        return DurableArtifactStore(
            index,
            blobs,
            ownership=ArtifactOwnership(session_name, project_name),
        )

    first = scoped("session-a", "project-a")
    project = await first.publish(
        _request(
            b"project",
            artifact_id="project-report",
            retention=ArtifactRetention.PROJECT,
        )
    )
    pinned = await first.publish(_request(b"pinned", artifact_id="pinned-report", retention=ArtifactRetention.PINNED))

    same_project = scoped("session-b", "project-a")
    other_project = scoped("session-c", "project-b")
    assert await same_project.read(project.get("svg")) == b"project"
    with pytest.raises(ArtifactNotFoundError):
        await other_project.get_revision(project.artifact_id, project.revision)
    assert await other_project.read(pinned.get("svg")) == b"pinned"


@pytest.mark.asyncio
async def test_cross_scope_promotion_preserves_idempotent_replay(tmp_path):
    store = DurableArtifactStore(
        tmp_path / "artifacts.sqlite3",
        MemoryBlobs(),
        ownership=ArtifactOwnership("session", "project"),
    )
    request = _request(
        b"promoted",
        idempotency_key="stable-call",
        retention=ArtifactRetention.SESSION,
    )
    original = await store.publish(request)

    promoted = await store.promote(
        original.artifact_id,
        original.revision,
        ArtifactRetention.PINNED,
    )
    replayed = await store.publish(request)

    assert replayed.artifact_id == promoted.artifact_id
    assert replayed.revision == promoted.revision
    assert replayed.get("svg").retention is ArtifactRetention.PINNED
    assert await store.read(original.get("svg")) == b"promoted"


@pytest.mark.asyncio
async def test_ephemeral_scope_releases_at_explicit_turn_boundary(tmp_path):
    store = DurableArtifactStore(
        tmp_path / "artifacts.sqlite3",
        MemoryBlobs(),
        ownership=ArtifactOwnership("session", "project"),
    )
    ephemeral = await store.publish(_request(b"turn", retention=ArtifactRetention.EPHEMERAL))
    session = await store.publish(_request(b"session"))

    assert await store.release(ephemeral.artifact_id, ephemeral.revision) is True
    with pytest.raises(ArtifactNotFoundError):
        await store.get_revision(ephemeral.artifact_id, ephemeral.revision)
    assert await store.read(session.get("svg")) == b"session"


@pytest.mark.asyncio
async def test_store_gc_reclaims_only_after_last_logical_root(tmp_path):
    repository = ContentAddressedArtifactStore(tmp_path / "project" / "blobs", hard_limit_bytes=1_024)
    project = DurableArtifactStore(
        tmp_path / "project" / "artifacts.sqlite3",
        ContentAddressedArtifactBlobStore(repository),
    )
    leases = InMemoryLeaseCoordinator()
    collector = ArtifactGarbageCollector(
        project, repository, lease_coordinator=leases, lease=leases.acquire("artifact-gc:test", "owner", 30)
    )
    first = await project.publish(
        _request(
            b"shared",
            artifact_id="first",
            retention=ArtifactRetention.PROJECT,
        )
    )
    second = await project.publish(
        _request(
            b"shared",
            artifact_id="second",
            retention=ArtifactRetention.PROJECT,
        )
    )
    digest = first.get("svg").digest

    assert await project.release(first.artifact_id, first.revision) is True
    collector.collect()
    assert digest in {item.identity.digest for item in repository.scan()}
    assert await project.read(second.get("svg")) == b"shared"

    assert await project.release(second.artifact_id, second.revision) is True
    collector.collect()
    assert digest not in {item.identity.digest for item in repository.scan()}


@pytest.mark.asyncio
async def test_artifact_gc_preserves_content_under_legal_hold_pin(tmp_path):
    class LegalHoldPins:
        def __init__(self):
            self.refs = ()

        def freeze_artifact_pins(self):
            return nullcontext(self.refs)

    repository = ContentAddressedArtifactStore(tmp_path / "blobs", hard_limit_bytes=1_024)
    store = DurableArtifactStore(
        tmp_path / "artifacts.sqlite3",
        ContentAddressedArtifactBlobStore(repository),
    )
    hold = LegalHoldPins()
    leases = InMemoryLeaseCoordinator()
    collector = ArtifactGarbageCollector(
        store,
        repository,
        pin_sources=(("legal-hold-fixture", hold),),
        lease_coordinator=leases,
        lease=leases.acquire("artifact-gc:test", "owner", 30),
    )
    revision = await store.publish(_request(b"held", retention=ArtifactRetention.PROJECT))
    ref = revision.get("svg")
    content_ref = ArtifactContentRef(ContentIdentity(ref.digest, ref.size), ref.content_ref)
    hold.refs = (content_ref,)
    assert await store.release(revision.artifact_id, revision.revision) is True

    assert collector.collect() == 0
    assert repository.read_bytes(content_ref) == b"held"

    hold.refs = ()
    assert collector.collect() == 1


@pytest.mark.asyncio
async def test_artifact_gc_formal_hold_blocks_claim_until_owner_releases(tmp_path):
    repository = ContentAddressedArtifactStore(tmp_path / "blobs", hard_limit_bytes=1_024)
    store = DurableArtifactStore(tmp_path / "artifacts.sqlite3", ContentAddressedArtifactBlobStore(repository))
    leases = InMemoryLeaseCoordinator()
    collector = ArtifactGarbageCollector(
        store, repository, lease_coordinator=leases, lease=leases.acquire("artifact-gc:test", "owner", 30)
    )
    revision = await store.publish(_request(b"held", retention=ArtifactRetention.PROJECT))
    ref = revision.get("svg")
    store.put_hold(ArtifactHold("legal-1", ArtifactHoldKind.LEGAL, ref.digest, "legal", 1))
    assert await store.release(revision.artifact_id, revision.revision) is True
    assert collector.collect() == 0
    assert store.release_hold("legal-1", owner_id="legal", expected_generation=1) is True
    assert collector.collect() == 1


@pytest.mark.asyncio
async def test_artifact_gc_recovers_in_doubt_after_blob_was_removed(tmp_path):
    class RemoveThenFailRepository(ContentAddressedArtifactStore):
        fail_once = True

        def reclaim(self, ref):
            removed = super().reclaim(ref)
            if self.fail_once:
                self.fail_once = False
                raise OSError("receipt commit lost")
            return removed

    class Blobs:
        def __init__(self, repository):
            self.repository = repository

        def put_bytes(self, content):
            return self.repository.put_bytes(content)

        def read_bytes(self, ref):
            return self.repository.read_bytes(ref)

    repository = RemoveThenFailRepository(tmp_path / "blobs", hard_limit_bytes=1_024)
    store = DurableArtifactStore(tmp_path / "artifacts.sqlite3", Blobs(repository))
    leases = InMemoryLeaseCoordinator()
    collector = ArtifactGarbageCollector(
        store, repository, lease_coordinator=leases, lease=leases.acquire("artifact-gc:test", "owner", 30)
    )
    revision = await store.publish(_request(b"uncertain", retention=ArtifactRetention.PROJECT))
    assert await store.release(revision.artifact_id, revision.revision) is True
    with pytest.raises(OSError, match="receipt commit lost"):
        collector.collect()
    assert len(store.scan_in_doubt_deletions()) == 1
    assert collector.collect() == 0
    assert store.scan_in_doubt_deletions() == ()


@pytest.mark.asyncio
async def test_lookup_visibility_never_exposes_partial_artifact_publication(tmp_path):
    store = DurableArtifactStore(tmp_path / "artifacts.sqlite3", MemoryBlobs())
    lookup_key = "sha256:" + "d" * 64

    assert await store.resolve_lookup(lookup_key) is None
    with pytest.raises(ArtifactNotFoundError):
        await store.publish_lookup(lookup_key, "reasoning", 1)
    assert await store.resolve_lookup(lookup_key) is None

    revision = await store.publish(_request(b"opaque-reasoning", artifact_id="reasoning"))
    assert await store.resolve_lookup(lookup_key) is None

    await store.publish_lookup(
        lookup_key,
        revision.artifact_id,
        revision.revision,
    )
    assert await store.resolve_lookup(lookup_key) == revision
    await store.publish_lookup(
        lookup_key,
        revision.artifact_id,
        revision.revision,
    )


@pytest.mark.asyncio
async def test_releasing_one_session_owner_preserves_another(tmp_path):
    blobs = MemoryBlobs()
    index = tmp_path / "artifacts.sqlite3"
    first = DurableArtifactStore(
        index,
        blobs,
        ownership=ArtifactOwnership("session-a", "project"),
    )
    second = DurableArtifactStore(
        index,
        blobs,
        ownership=ArtifactOwnership("session-b", "project"),
    )
    request = _request(b"shared", idempotency_key="same-call")

    revision = await first.publish(request)
    assert await second.publish(request) == revision

    assert await first.release(revision.artifact_id, revision.revision) is True
    with pytest.raises(ArtifactNotFoundError):
        await first.get_revision(revision.artifact_id, revision.revision)
    assert await second.read(revision.get("svg")) == b"shared"
