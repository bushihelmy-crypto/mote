from __future__ import annotations

import hashlib

import pytest

from mote.contracts.artifact import (
    ArtifactContentRef,
    ArtifactPublicationIntent,
    ArtifactRepresentationInput,
    ArtifactRepresentationIntent,
)
from mote.contracts.content import ContentIdentity
from mote.contracts.runtime import (
    CheckpointFidelity,
    RuntimeCheckpoint,
    RuntimeProjectionIntent,
    RuntimeProjectionRequest,
)
from mote.contracts.surface import (
    CanvasDocument,
    CanvasElement,
    CanvasExportRepresentation,
    NotebookCell,
    NotebookDocument,
)
from mote.runtime.artifacts import DurableArtifactStore, ReliableArtifactPublisher
from mote.runtime.interactive.checkpoint_codec import encode_inline_json
from mote.runtime.projections import (
    CanvasArtifactProjector,
    NotebookArtifactProjector,
    RuntimeProjectionReconciler,
    RuntimeProjectionRegistry,
    materialize_artifact_projection,
)


class MemoryBlobs:
    def __init__(self) -> None:
        self.data: dict[str, bytes] = {}

    def put_bytes(self, content: bytes) -> ArtifactContentRef:
        digest = hashlib.sha256(content).hexdigest()
        self.data[digest] = content
        return ArtifactContentRef(
            identity=ContentIdentity(digest, len(content)),
            locator=f"sha256:{digest}",
        )

    def read_bytes(self, ref: ArtifactContentRef) -> bytes:
        return self.data[ref.identity.digest]


class Journal:
    def __init__(self) -> None:
        self.acks = []

    async def acknowledge(self, ack) -> None:
        self.acks.append(ack)

    async def record_commit(self, fact) -> None:
        raise AssertionError("not used by reconcile")


class Projector:
    projector = "test-artifact"
    schema_version = 1

    def __init__(self, publication: ArtifactPublicationIntent) -> None:
        self.publication = publication

    async def project(self, request) -> ArtifactPublicationIntent:
        return self.publication


class FailingProjector:
    projector = "failure"
    schema_version = 1

    async def project(self, request) -> ArtifactPublicationIntent:
        raise ValueError("cannot decode checkpoint")


def _request(
    *,
    projector: str = "test-artifact",
    schema_version: int = 1,
    intent_id: str = "export",
) -> RuntimeProjectionRequest:
    return RuntimeProjectionRequest(
        commit_id=f"commit-{intent_id}",
        checkpoint=RuntimeCheckpoint(
            runtime_id="runtime-1",
            kind="test",
            epoch=1,
            revision=2,
            codec="test+json@1",
            schema_version=1,
            payload_ref="memory:test",
            fidelity=CheckpointFidelity.FULL,
        ),
        intent=RuntimeProjectionIntent(
            intent_id=intent_id,
            projector=projector,
            schema_version=schema_version,
        ),
    )


def _publication(blobs: MemoryBlobs) -> ArtifactPublicationIntent:
    content = blobs.put_bytes(b"projection")
    return ArtifactPublicationIntent(
        publication_id="projection-artifact",
        artifact_id="projection-artifact",
        expected_revision=0,
        representations=(
            ArtifactRepresentationIntent(
                representation="text",
                kind="document",
                mime_type="text/plain",
                content=content,
            ),
        ),
    )


def _checkpoint(kind: str, payload, *, codec: str) -> RuntimeCheckpoint:
    encoded = encode_inline_json(
        payload,
        codec=codec,
        fidelity=CheckpointFidelity.FULL,
    )
    return RuntimeCheckpoint(
        runtime_id=f"{kind}-1",
        kind=kind,
        epoch=1,
        revision=3,
        codec=encoded.codec,
        schema_version=encoded.schema_version,
        payload_ref=encoded.payload_ref,
        digest=encoded.digest,
        fidelity=encoded.fidelity or CheckpointFidelity.FULL,
    )


@pytest.mark.asyncio
async def test_materializer_seals_bytes_and_publishes_without_copy(tmp_path):
    blobs = MemoryBlobs()
    intent = await materialize_artifact_projection(
        blobs,
        (
            ArtifactRepresentationInput(
                representation="svg",
                kind="canvas",
                mime_type="image/svg+xml",
                content=b"<svg/>",
                suggested_name="canvas.svg",
            ),
        ),
        identity_representation="svg",
        artifact_prefix="canvas",
    )
    store = DurableArtifactStore(tmp_path / "artifacts.sqlite3", blobs)

    revision = await ReliableArtifactPublisher(store, store).publish_intent(intent)

    assert revision.artifact_id == intent.artifact_id
    assert await store.read(revision.get("svg")) == b"<svg/>"


@pytest.mark.asyncio
async def test_reconciler_isolates_failures_and_acknowledges_only_published(tmp_path):
    blobs = MemoryBlobs()
    store = DurableArtifactStore(tmp_path / "artifacts.sqlite3", blobs)
    publisher = ReliableArtifactPublisher(store, store)
    journal = Journal()
    registry = RuntimeProjectionRegistry()
    registry.register(Projector(_publication(blobs)))
    registry.register(FailingProjector())
    reconciler = RuntimeProjectionReconciler(registry, journal, publisher)
    healthy = _request(intent_id="healthy")
    broken = _request(projector="failure", intent_id="broken")
    missing = _request(projector="missing", intent_id="missing")

    result = await reconciler.reconcile((broken, healthy, missing))

    assert [ack.key for ack in result.completed] == [(healthy.commit_id, healthy.intent.intent_id)]
    assert [ack for ack in journal.acks if ack.status == "completed"] == list(result.completed)
    assert result.failed == ()
    assert {failure.intent_id for failure in result.dead_lettered} == {
        "broken",
        "missing",
    }
    assert {ack.status for ack in journal.acks if ack.status != "completed"} == {"dead_letter"}


def test_registry_requires_exact_schema_and_rejects_duplicates():
    blobs = MemoryBlobs()
    registry = RuntimeProjectionRegistry()
    projector = Projector(_publication(blobs))
    registry.register(projector)

    assert registry.resolve(_request()) is projector
    with pytest.raises(LookupError, match="not registered"):
        registry.resolve(_request(schema_version=2))
    with pytest.raises(ValueError, match="already registered"):
        registry.register(projector)


@pytest.mark.asyncio
async def test_canvas_projector_rebuilds_svg_from_checkpoint(tmp_path):
    blobs = MemoryBlobs()
    document = CanvasDocument(
        elements=[
            CanvasElement(
                id="node-a",
                kind="rect",
                x=10,
                y=20,
                width=100,
                height=50,
            )
        ]
    )
    request = RuntimeProjectionRequest(
        commit_id="canvas-commit",
        checkpoint=_checkpoint(
            "canvas",
            document.model_dump(mode="json"),
            codec="canvas-document+json@1",
        ),
        intent=RuntimeProjectionIntent(
            intent_id="artifact",
            projector="canvas-artifact",
            schema_version=1,
        ),
    )
    intent = await CanvasArtifactProjector(blobs).project(request)
    store = DurableArtifactStore(tmp_path / "canvas.sqlite3", blobs)

    revision = await ReliableArtifactPublisher(store, store).publish_intent(intent)

    assert intent.artifact_id.startswith("canvas-")
    assert b'id="node-a"' in await store.read(revision.get("svg"))


@pytest.mark.asyncio
async def test_canvas_projector_replays_requested_headless_export_set(tmp_path):
    class Exports:
        async def export(self, document, formats):
            assert formats == ("svg", "png", "drawio")
            return tuple(
                CanvasExportRepresentation(
                    representation=format,
                    mime_type={
                        "svg": "image/svg+xml",
                        "png": "image/png",
                        "drawio": "application/vnd.jgraph.mxfile",
                    }[format],
                    content=f"{format}:{len(document.elements)}".encode(),
                    suggested_name=f"canvas.{format}",
                )
                for format in formats
            )

    blobs = MemoryBlobs()
    document = CanvasDocument(elements=[CanvasElement(id="node", kind="rect")])
    request = RuntimeProjectionRequest(
        commit_id="canvas-multi-export",
        checkpoint=_checkpoint(
            "canvas",
            document.model_dump(mode="json"),
            codec="canvas-document+json@1",
        ),
        intent=RuntimeProjectionIntent(
            intent_id="artifact",
            projector="canvas-artifact",
            schema_version=1,
            options=(("formats", "svg,png,drawio"),),
        ),
    )

    intent = await CanvasArtifactProjector(blobs, Exports()).project(request)

    assert {item.representation for item in intent.representations} == {
        "svg",
        "png",
        "drawio",
    }


@pytest.mark.asyncio
async def test_notebook_projector_rebuilds_ipynb_from_v2_checkpoint(tmp_path):
    blobs = MemoryBlobs()
    document = NotebookDocument(
        ref="jupyter-session:test",
        revision=4,
        cells=[NotebookCell(id="cell-1", source="answer = 42")],
    )
    request = RuntimeProjectionRequest(
        commit_id="notebook-commit",
        checkpoint=_checkpoint(
            "jupyter",
            {
                "cwd": "/workspace",
                "env": {},
                "unset": [],
                "notebook": document.model_dump(mode="json"),
            },
            codec="jupyter-state+json@2",
        ),
        intent=RuntimeProjectionIntent(
            intent_id="artifact",
            projector="notebook-artifact",
            schema_version=1,
        ),
    )
    intent = await NotebookArtifactProjector(blobs).project(request)
    store = DurableArtifactStore(tmp_path / "notebook.sqlite3", blobs)

    revision = await ReliableArtifactPublisher(store, store).publish_intent(intent)

    content = await store.read(revision.get("ipynb"))
    assert intent.artifact_id.startswith("notebook-")
    assert b'"source":"answer = 42"' in content


@pytest.mark.asyncio
async def test_notebook_projector_rejects_legacy_checkpoint_without_document():
    blobs = MemoryBlobs()
    request = RuntimeProjectionRequest(
        commit_id="legacy-notebook-commit",
        checkpoint=_checkpoint(
            "jupyter",
            {"cwd": "/workspace", "env": {}, "unset": []},
            codec="jupyter-state+json@1",
        ),
        intent=RuntimeProjectionIntent(
            intent_id="artifact",
            projector="notebook-artifact",
            schema_version=1,
        ),
    )

    with pytest.raises(ValueError, match="unsupported checkpoint codec"):
        await NotebookArtifactProjector(blobs).project(request)
