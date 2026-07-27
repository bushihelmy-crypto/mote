from __future__ import annotations

import hashlib

import pytest

from mote.contracts.artifacts import ArtifactContentRef
from mote.contracts.canvas import CanvasDocument, CanvasElement, CanvasExportRepresentation, CanvasOperation
from mote.contracts.handoff import HandoffOutcome, HandoffStatus
from mote.contracts.runtimes import RuntimeAccessMode, RuntimeRef, RuntimeState
from mote.product.toolsets.builtin.canvas import Canvas
from mote.runtime.artifacts import DurableArtifactStore, ReliableArtifactPublisher
from mote.runtime.fileops import FileOperations
from mote.runtime.interactive import RuntimeHost
from mote.runtime.tools.dependency._canvas import CanvasRuntimeDriver
from mote.runtime.tools.tool_result import ToolError


class MemoryBlobs:
    def __init__(self) -> None:
        self.contents: dict[str, bytes] = {}

    def put_bytes(self, content: bytes) -> ArtifactContentRef:
        digest = hashlib.sha256(content).hexdigest()
        self.contents[digest] = content
        return ArtifactContentRef(
            content_ref=f"sha256:{digest}",
            digest=digest,
            size=len(content),
        )

    def read_bytes(self, ref: ArtifactContentRef) -> bytes:
        return self.contents[ref.digest]


class FailingArtifactPublisher:
    async def publish(self, publication_id, request):
        raise OSError("artifact index unavailable")


def _publisher(store):
    return ReliableArtifactPublisher(store, store)


def test_canvas_is_preapproved_for_every_action():
    tool = Canvas()

    for args in (
        {"operations": []},
        {"operations": [{"op": "clear"}]},
        {"operations": [], "close": True},
        {"action": "handoff"},
        {"operations": [], "export_formats": ["png"], "output_dir": "exports"},
    ):
        decision = tool.check_permissions(args)
        assert decision is not None
        assert decision.behavior == "allow"


@pytest.mark.asyncio
async def test_canvas_handoff_returns_current_document_to_the_model():
    host = RuntimeHost()
    driver = CanvasRuntimeDriver()
    descriptor = await host.create(driver, runtime_id="canvas-default")
    async with host.access(descriptor.ref, mode=RuntimeAccessMode.WRITE, owner_id="human:test") as access:
        driver.apply(
            [
                CanvasOperation(
                    op="upsert",
                    element=CanvasElement(id="human-added", kind="text", x=20, y=30, text="Changed"),
                )
            ]
        )
        access.commit(changed=True)

    tool = Canvas().bind("canvas-handoff-test")
    tool.get_runtime_host = lambda: host

    async def handoff_runtime(runtime: str, *, message: str = "") -> HandoffOutcome:
        assert runtime == "canvas:default"
        assert message == "Please update the drawing"
        return HandoffOutcome(
            status=HandoffStatus.COMPLETED,
            runtime_ref=RuntimeRef(runtime_id="canvas-default", kind="canvas"),
            from_revision=0,
            to_revision=1,
            human_message="Updated the label",
        )

    tool.handoff_runtime = handoff_runtime
    result = await tool.call(action="handoff", message="Please update the drawing")

    assert result.success
    assert result.data.elements[0].id == "human-added"
    assert "Current canvas after handoff (revision 1):" in result.output
    assert '"id":"human-added"' in result.output
    await tool.cleanup_session(tool.session_id)


class MultiRepresentationCanvasDriver(CanvasRuntimeDriver):
    requested_formats: tuple[str, ...] = ()

    async def export_representations(
        self,
        document: CanvasDocument,
        formats: tuple[str, ...] = ("svg",),
    ) -> tuple[CanvasExportRepresentation, ...]:
        self.requested_formats = formats
        svg = (await super().export_representations(document))[0]
        return (
            svg,
            CanvasExportRepresentation(
                representation="png",
                mime_type="image/png",
                content=b"fake-png",
                suggested_name="canvas.png",
            ),
            CanvasExportRepresentation(
                representation="drawio",
                mime_type="application/vnd.jgraph.mxfile",
                content=b"<mxfile/>",
                suggested_name="canvas.drawio",
            ),
        )


class FailingExportCanvasDriver(CanvasRuntimeDriver):
    async def export_representations(
        self,
        document: CanvasDocument,
        formats: tuple[str, ...] = ("svg",),
    ) -> tuple[CanvasExportRepresentation, ...]:
        raise OSError("headless renderer unavailable")


@pytest.mark.asyncio
async def test_canvas_tool_batches_operations_without_exposing_internal_artifact(
    tmp_path,
):
    host = RuntimeHost()
    artifacts = DurableArtifactStore(tmp_path / "artifacts.sqlite3", MemoryBlobs())
    tool = Canvas().bind("canvas-tool-test")
    tool.get_runtime_host = lambda: host
    tool.get_artifact_publisher = lambda: _publisher(artifacts)

    result = await tool.call(
        operations=[
            {
                "op": "upsert",
                "element": {
                    "id": "node-a",
                    "kind": "ellipse",
                    "x": 50,
                    "y": 60,
                    "width": 180,
                    "height": 90,
                },
            }
        ],
        width=800,
        height=600,
    )

    assert result.success
    assert result.data.width == 800
    assert result.data.elements[0].id == "node-a"
    assert result.media == []
    assert result.artifacts == []
    assert "artifact:" not in result.output
    assert host.descriptor("canvas:default").revision == 1
    await tool.cleanup_session(tool.session_id)


@pytest.mark.asyncio
async def test_invalid_canvas_batch_fails_before_runtime_creation(tmp_path):
    host = RuntimeHost()
    artifacts = DurableArtifactStore(tmp_path / "artifacts.sqlite3", MemoryBlobs())
    tool = Canvas().bind("canvas-invalid-test")
    tool.get_runtime_host = lambda: host
    tool.get_artifact_publisher = lambda: _publisher(artifacts)

    with pytest.raises(ToolError):
        await tool.call(operations=[{"op": "upsert"}])

    assert host.list() == []


@pytest.mark.asyncio
async def test_canvas_export_formats_require_local_directory(tmp_path):
    host = RuntimeHost()
    tool = Canvas().bind("canvas-export-target-test")
    tool.get_runtime_host = lambda: host

    with pytest.raises(ToolError):
        await tool.call(operations=[], export_formats=["png"])

    assert host.list() == []


@pytest.mark.asyncio
async def test_canvas_artifact_failure_reports_committed_partial_success():
    host = RuntimeHost()
    tool = Canvas().bind("canvas-artifact-failure-test")
    tool.get_runtime_host = lambda: host
    tool.get_artifact_publisher = FailingArtifactPublisher

    with pytest.raises(ToolError) as raised:
        await tool.call(
            operations=[
                {
                    "op": "upsert",
                    "element": {
                        "id": "committed-box",
                        "kind": "rect",
                        "width": 10,
                        "height": 10,
                    },
                }
            ]
        )

    error = raised.value
    assert error.context["partial_success"] is True
    assert error.context["committed_revision"] == 1
    assert error.context["failed_stage"] == "artifact_publish"
    assert "Do not replay the operations" in str(error)
    assert host.descriptor("canvas:default").revision == 1
    async with host.access(
        "canvas:default",
        mode=RuntimeAccessMode.READ,
        owner_id="agent:test:verify-partial-success",
    ) as access:
        driver = access.driver
        assert isinstance(driver, CanvasRuntimeDriver)
        assert driver.snapshot_document().elements[0].id == "committed-box"
    await tool.cleanup_session(tool.session_id)


@pytest.mark.asyncio
async def test_canvas_exports_files_to_local_directory(tmp_path):
    host = RuntimeHost()
    artifacts = DurableArtifactStore(
        tmp_path / "canvas-artifacts.sqlite3",
        MemoryBlobs(),
    )
    driver = MultiRepresentationCanvasDriver()
    await host.ensure(driver)
    file_operations = FileOperations(
        session_id="canvas-local-export-test",
        journal_path=tmp_path / "session" / "rollout.jsonl",
        get_project_root=lambda: str(tmp_path),
        lock_root=tmp_path / "locks",
    )
    output_dir = tmp_path / "exports"
    output_dir.mkdir()

    async def commit_generated_files(files, *, source):
        return file_operations.commit_generated_files(files, source=source)

    tool = Canvas().bind("canvas-local-export-test")
    tool.get_runtime_host = lambda: host
    tool.get_artifact_publisher = lambda: _publisher(artifacts)
    tool.get_cwd = lambda: str(tmp_path)
    tool.commit_generated_files = commit_generated_files

    result = await tool.call(
        operations=[],
        export_formats=["png", "drawio"],
        output_dir="exports",
    )

    assert (output_dir / "canvas.png").read_bytes() == b"fake-png"
    assert (output_dir / "canvas.drawio").read_bytes() == b"<mxfile/>"
    assert result.media[0].ref == str(output_dir / "canvas.png")
    await tool.cleanup_session(tool.session_id)


@pytest.mark.asyncio
async def test_canvas_export_failure_occurs_after_runtime_commit(tmp_path):
    host = RuntimeHost()
    artifacts = DurableArtifactStore(tmp_path / "artifacts.sqlite3", MemoryBlobs())
    await host.ensure(FailingExportCanvasDriver())
    tool = Canvas().bind("canvas-export-failure-test")
    tool.get_runtime_host = lambda: host
    tool.get_artifact_publisher = lambda: _publisher(artifacts)

    with pytest.raises(ToolError) as raised:
        await tool.call(
            operations=[
                {
                    "op": "upsert",
                    "element": {
                        "id": "survives-export-failure",
                        "kind": "rect",
                        "width": 10,
                        "height": 10,
                    },
                }
            ]
        )

    error = raised.value
    descriptor = host.descriptor("canvas:default")
    assert descriptor.revision == 1
    assert descriptor.state is RuntimeState.READY
    assert error.context["partial_success"] is True
    assert error.context["committed_revision"] == 1
    assert error.context["failed_stage"] == "canvas_export"
    async with host.access(
        descriptor.ref,
        mode=RuntimeAccessMode.READ,
        owner_id="agent:test:verify-export-failure",
    ) as access:
        driver = access.driver
        assert isinstance(driver, CanvasRuntimeDriver)
        assert driver.snapshot_document().elements[0].id == "survives-export-failure"
    await tool.cleanup_session(tool.session_id)
