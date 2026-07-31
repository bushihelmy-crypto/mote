from __future__ import annotations

import asyncio
import hashlib

import pytest

from mote.contracts.artifact import (
    ArtifactContentRef,
    ArtifactPublicationIntent,
    ArtifactPublicationState,
    ArtifactPublishRequest,
    ArtifactRepresentationInput,
    ArtifactRepresentationIntent,
)
from mote.contracts.artifact.errors import (
    ArtifactIdempotencyConflictError,
    ArtifactNotFoundError,
    ArtifactPublicationTerminalError,
    ArtifactRevisionConflictError,
)
from mote.contracts.content import ContentIdentity
from mote.contracts.ports.artifact.store import ArtifactPublicationOutbox
from mote.contracts.ports.artifact.store import ReliableArtifactPublisher as ReliableArtifactPublisherPort
from mote.runtime.artifacts import ArtifactRepositoryBlobStore, DurableArtifactStore, ReliableArtifactPublisher
from mote.runtime.artifacts.repository import ArtifactRepository


class MemoryBlobs:
    def __init__(self) -> None:
        self.contents: dict[str, bytes] = {}

    def put_bytes(self, content: bytes) -> ArtifactContentRef:
        digest = hashlib.sha256(content).hexdigest()
        self.contents[digest] = content
        return ArtifactContentRef(
            identity=ContentIdentity(digest, len(content)),
            locator=f"sha256:{digest}",
        )

    def read_bytes(self, ref: ArtifactContentRef) -> bytes:
        content = self.contents[ref.identity.digest]
        assert hashlib.sha256(content).hexdigest() == ref.identity.digest
        assert len(content) == ref.identity.size
        return content


def _request(
    content: bytes,
    *,
    artifact_id: str = "",
    expected_revision: int | None = None,
    idempotency_key: str = "",
) -> ArtifactPublishRequest:
    return ArtifactPublishRequest(
        artifact_id=artifact_id,
        expected_revision=expected_revision,
        idempotency_key=idempotency_key,
        representations=(
            ArtifactRepresentationInput(
                representation="svg",
                kind="diagram",
                mime_type="image/svg+xml",
                content=content,
                suggested_name="diagram.svg",
            ),
        ),
    )


def _intent(
    publication_id: str,
    content: ArtifactContentRef,
    *,
    artifact_id: str = "",
    expected_revision: int | None = None,
) -> ArtifactPublicationIntent:
    return ArtifactPublicationIntent(
        publication_id=publication_id,
        artifact_id=artifact_id,
        expected_revision=expected_revision,
        representations=(
            ArtifactRepresentationIntent(
                representation="svg",
                kind="diagram",
                mime_type="image/svg+xml",
                content=content,
                suggested_name="diagram.svg",
            ),
        ),
    )


def test_publication_implementations_satisfy_public_ports(tmp_path):
    store = DurableArtifactStore(tmp_path / "artifacts.sqlite3", MemoryBlobs())
    publisher = ReliableArtifactPublisher(store, store)

    assert isinstance(store, ArtifactPublicationOutbox)
    assert isinstance(publisher, ReliableArtifactPublisherPort)


@pytest.mark.asyncio
async def test_staged_publication_survives_store_restart(tmp_path):
    blobs = MemoryBlobs()
    path = tmp_path / "artifacts.sqlite3"
    store = DurableArtifactStore(path, blobs)

    staged = await store.stage("runtime:surface:7", _request(b"<svg/>"))
    restarted = DurableArtifactStore(path, blobs)
    pending_ids = await restarted.pending_ids()

    assert staged.state is ArtifactPublicationState.QUEUED
    expected_key = "artifact-publication:" + hashlib.sha256(b"runtime:surface:7").hexdigest()
    assert staged.request.idempotency_key == expected_key
    assert pending_ids == (staged.publication_id,)
    assert await restarted.load(staged.publication_id) == staged


@pytest.mark.asyncio
async def test_materialized_intent_stages_existing_cas_without_copying(tmp_path):
    blobs = MemoryBlobs()
    content = blobs.put_bytes(b"<svg>materialized</svg>")
    initial_blob_count = len(blobs.contents)
    path = tmp_path / "artifacts.sqlite3"
    store = DurableArtifactStore(path, blobs)

    staged = await store.stage_intent(_intent("runtime:commit:7", content))
    restarted = DurableArtifactStore(path, blobs)

    assert len(blobs.contents) == initial_blob_count
    assert staged.request.representations[0].content == b"<svg>materialized</svg>"
    assert await restarted.load(staged.publication_id) == staged


@pytest.mark.asyncio
async def test_materialized_intent_publishes_through_reliable_publisher(tmp_path):
    blobs = MemoryBlobs()
    content = blobs.put_bytes(b"<svg>projected</svg>")
    store = DurableArtifactStore(tmp_path / "artifacts.sqlite3", blobs)

    revision = await ReliableArtifactPublisher(store, store).publish_intent(_intent("runtime:commit:8", content))

    assert await store.read(revision.get("svg")) == b"<svg>projected</svg>"
    assert await store.pending_ids() == ()


@pytest.mark.asyncio
async def test_materialized_intent_rejects_untrusted_cas_metadata(tmp_path):
    class UnverifiedBlobs(MemoryBlobs):
        def read_bytes(self, ref: ArtifactContentRef) -> bytes:
            return b"different"

    blobs = UnverifiedBlobs()
    claimed = ArtifactContentRef(
        identity=ContentIdentity(hashlib.sha256(b"claimed").hexdigest(), len(b"claimed")),
        locator=f"sha256:{hashlib.sha256(b'claimed').hexdigest()}",
    )
    store = DurableArtifactStore(tmp_path / "artifacts.sqlite3", blobs)

    with pytest.raises(ValueError, match="does not match"):
        await store.stage_intent(_intent("runtime:commit:forged", claimed))

    assert await store.pending_ids() == ()


@pytest.mark.asyncio
async def test_reconcile_replays_publish_after_crash_before_ack(tmp_path):
    blobs = MemoryBlobs()
    path = tmp_path / "artifacts.sqlite3"
    store = DurableArtifactStore(path, blobs)
    staged = await store.stage("runtime:surface:8", _request(b"<svg>8</svg>"))
    first_revision = await store.publish(staged.request)

    restarted = DurableArtifactStore(path, blobs)
    result = await ReliableArtifactPublisher(
        restarted,
        restarted,
    ).reconcile_pending()

    assert result.failed == ()
    assert result.published[0].revision == first_revision
    assert await restarted.pending_ids() == ()
    completed = await restarted.stage("runtime:surface:8", _request(b"<svg>8</svg>"))
    assert completed.state is ArtifactPublicationState.COMPLETED
    assert completed.result_revision == 1


@pytest.mark.asyncio
async def test_concurrent_reconcilers_publish_one_revision(tmp_path):
    blobs = MemoryBlobs()
    path = tmp_path / "artifacts.sqlite3"
    store = DurableArtifactStore(path, blobs)
    await store.stage("runtime:surface:9", _request(b"<svg>9</svg>"))
    restarted = DurableArtifactStore(path, blobs)
    first = ReliableArtifactPublisher(store, store)
    second = ReliableArtifactPublisher(restarted, restarted)

    outcomes = await asyncio.gather(
        first.reconcile_pending(),
        second.reconcile_pending(),
    )

    assert all(not item.failed for item in outcomes)
    completed = await store.stage("runtime:surface:9", _request(b"<svg>9</svg>"))
    assert completed.state is ArtifactPublicationState.COMPLETED
    assert completed.result_revision == 1
    assert (await store.get_revision(completed.result_artifact_id, 1)).revision == 1


@pytest.mark.asyncio
async def test_acknowledge_rejects_unrelated_durable_revision(tmp_path):
    store = DurableArtifactStore(tmp_path / "artifacts.sqlite3", MemoryBlobs())
    await store.stage("runtime:surface:10", _request(b"staged"))
    unrelated = await store.publish(_request(b"unrelated", idempotency_key="unrelated-publication"))

    with pytest.raises(ArtifactIdempotencyConflictError):
        await store.acknowledge("runtime:surface:10", unrelated)

    assert len(await store.pending_ids()) == 1


@pytest.mark.asyncio
async def test_acknowledge_is_idempotent_for_the_same_revision(tmp_path):
    store = DurableArtifactStore(tmp_path / "artifacts.sqlite3", MemoryBlobs())
    publisher = ReliableArtifactPublisher(store, store)
    revision = await publisher.publish("runtime:surface:11", _request(b"same"))

    await store.acknowledge("runtime:surface:11", revision)

    completed = await store.stage("runtime:surface:11", _request(b"same"))
    assert completed.state is ArtifactPublicationState.COMPLETED
    assert completed.attempts == 1


@pytest.mark.asyncio
async def test_acknowledge_validates_metadata_without_reopening_cas(
    tmp_path,
    monkeypatch,
):
    blobs = MemoryBlobs()
    store = DurableArtifactStore(tmp_path / "artifacts.sqlite3", blobs)
    staged = await store.stage("runtime:surface:metadata-ack", _request(b"sealed"))
    revision = await store.publish(staged.request)

    def forbidden_blob_access(*args, **kwargs):
        raise AssertionError("acknowledgement touched blob storage")

    monkeypatch.setattr(blobs, "put_bytes", forbidden_blob_access)
    monkeypatch.setattr(blobs, "read_bytes", forbidden_blob_access)
    await store.acknowledge("runtime:surface:metadata-ack", revision)

    assert await store.pending_ids() == ()


@pytest.mark.asyncio
async def test_reconcile_dead_letters_immutable_revision_conflict(tmp_path):
    store = DurableArtifactStore(tmp_path / "artifacts.sqlite3", MemoryBlobs())
    await store.publish(_request(b"existing", artifact_id="fixed"))
    await store.stage(
        "runtime:surface:conflict",
        _request(b"conflict", artifact_id="fixed", expected_revision=0),
    )

    result = await ReliableArtifactPublisher(store, store).reconcile_pending()

    assert result.published == ()
    assert result.failed == ()
    assert result.dead_lettered[0].publication_id == "runtime:surface:conflict"
    assert "ArtifactRevisionConflictError" in result.dead_lettered[0].error
    assert await store.pending_ids() == ()
    pending = await store.load("runtime:surface:conflict")
    assert pending.state is ArtifactPublicationState.DEAD_LETTER
    assert pending.attempts == 1


@pytest.mark.asyncio
async def test_dead_letter_is_terminal_for_ordinary_publish(tmp_path):
    store = DurableArtifactStore(tmp_path / "artifacts.sqlite3", MemoryBlobs())
    request = _request(
        b"must-not-publish",
        artifact_id="dead-artifact",
        expected_revision=0,
    )
    staged = await store.stage("runtime:surface:dead", request)
    await store.dead_letter(staged.publication_id, "permanent")

    with pytest.raises(ArtifactPublicationTerminalError, match="dead-lettered"):
        await ReliableArtifactPublisher(store, store).publish(
            staged.publication_id,
            request,
        )

    dead = await store.load(staged.publication_id)
    assert dead.state is ArtifactPublicationState.DEAD_LETTER
    assert dead.attempts == 1
    with pytest.raises(ArtifactNotFoundError):
        await store.get_revision("dead-artifact", 1)


@pytest.mark.asyncio
async def test_immediate_reliable_publish_records_failure_before_raising(tmp_path):
    store = DurableArtifactStore(tmp_path / "artifacts.sqlite3", MemoryBlobs())
    await store.publish(_request(b"existing", artifact_id="fixed-immediate"))
    publisher = ReliableArtifactPublisher(store, store)

    with pytest.raises(ArtifactRevisionConflictError, match="artifact revision changed"):
        await publisher.publish(
            "runtime:surface:immediate-conflict",
            _request(
                b"conflict",
                artifact_id="fixed-immediate",
                expected_revision=0,
            ),
        )

    failed = await store.load("runtime:surface:immediate-conflict")
    assert failed.state is ArtifactPublicationState.FAILED
    assert failed.attempts == 1


@pytest.mark.asyncio
async def test_corrupt_staged_cas_isolated_as_dead_letter(tmp_path):
    blobs = MemoryBlobs()
    store = DurableArtifactStore(tmp_path / "artifacts.sqlite3", blobs)
    staged = await store.stage("runtime:surface:corrupt", _request(b"recoverable"))
    await store.stage("runtime:surface:healthy", _request(b"healthy"))
    digest = hashlib.sha256(b"recoverable").hexdigest()
    content = blobs.contents.pop(digest)

    first = await ReliableArtifactPublisher(store, store).reconcile_pending()

    assert first.dead_lettered[0].publication_id == staged.publication_id
    assert "KeyError" in first.dead_lettered[0].error
    assert first.published[0].publication_id == "runtime:surface:healthy"
    assert await store.pending_ids() == ()

    blobs.contents[digest] = content
    result = await ReliableArtifactPublisher(store, store).reconcile_pending()
    assert result.published == ()


@pytest.mark.asyncio
async def test_publication_id_is_an_idempotency_boundary(tmp_path):
    store = DurableArtifactStore(tmp_path / "artifacts.sqlite3", MemoryBlobs())
    await store.stage("runtime:surface:12", _request(b"first"))

    with pytest.raises(ArtifactIdempotencyConflictError):
        await store.stage("runtime:surface:12", _request(b"different"))


@pytest.mark.asyncio
async def test_maximum_publication_id_derives_a_bounded_idempotency_key(tmp_path):
    store = DurableArtifactStore(tmp_path / "artifacts.sqlite3", MemoryBlobs())

    staged = await store.stage("p" * 256, _request(b"bounded-key"))

    assert len(staged.request.idempotency_key) < 256


@pytest.mark.asyncio
async def test_real_repository_adapter_uses_reserved_durable_publication(tmp_path):
    repository = ArtifactRepository(
        tmp_path / "blobs",
        hard_limit_bytes=1_024,
    )
    store = DurableArtifactStore(
        tmp_path / "artifacts.sqlite3",
        ArtifactRepositoryBlobStore(repository),
    )

    revision = await ReliableArtifactPublisher(store, store).publish(
        "runtime:report:repository",
        _request(b"repository-backed"),
    )

    ref = revision.get("svg")
    assert await store.read(ref) == b"repository-backed"
    assert {item.identity.digest: item.identity.size for item in repository.scan()}[ref.digest] == len(
        b"repository-backed"
    )


@pytest.mark.asyncio
async def test_sqlite_failure_leaves_live_orphan_never_aborted_blob(
    tmp_path,
    monkeypatch,
):
    content = b"durable-before-index"
    repository = ArtifactRepository(
        tmp_path / "blobs",
        hard_limit_bytes=1_024,
    )
    store = DurableArtifactStore(
        tmp_path / "artifacts.sqlite3",
        ArtifactRepositoryBlobStore(repository),
    )

    def fail_index_open():
        raise OSError("injected index failure")

    monkeypatch.setattr(store, "_connect", fail_index_open)
    with pytest.raises(OSError, match="injected index failure"):
        await store.publish(_request(content))

    digest = hashlib.sha256(content).hexdigest()
    artifact = next(item for item in repository.scan() if item.identity.digest == digest)
    assert repository.read_bytes(artifact) == content
