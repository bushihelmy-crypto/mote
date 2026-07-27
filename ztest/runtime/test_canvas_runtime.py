from __future__ import annotations

import asyncio
import hashlib
import json

import pytest

from mote.contracts.artifacts import ArtifactContentRef
from mote.contracts.canvas import CanvasDocument, CanvasElement, CanvasOperation, CanvasStyle
from mote.contracts.handoff import HandoffRequest, HandoffStatus, HumanHandoffOutcome
from mote.contracts.runtimes import RuntimeCheckpoint, RuntimeOperationIntent, RuntimeProjectionIntent, RuntimeRef
from mote.contracts.surfaces import SurfaceInput, SurfacePresentationMode
from mote.runtime.artifacts import DurableArtifactStore
from mote.runtime.interactive import ArtifactCheckpointPayloadStore, HandoffCoordinator, RuntimeHost
from mote.runtime.secrets.cipher import AesGcmCipher
from mote.runtime.session import SessionLog, SessionMetaEvent, SessionRuntimeProjectionJournal
from mote.runtime.session.replay import replay
from mote.runtime.session.runtime_operation import SessionRuntimeOperationJournal
from mote.runtime.tools.dependency._canvas import CanvasRuntimeDriver


class _MemoryBlobs:
    def __init__(self) -> None:
        self.contents: dict[str, bytes] = {}

    def put_bytes(self, content: bytes) -> ArtifactContentRef:
        digest = hashlib.sha256(content).hexdigest()
        self.contents[digest] = content
        return ArtifactContentRef(content_ref=f"sha256:{digest}", digest=digest, size=len(content))

    def read_bytes(self, ref: ArtifactContentRef) -> bytes:
        return self.contents[ref.digest]


@pytest.mark.asyncio
async def test_canvas_batch_is_atomic_and_checkpoint_restores():
    host = RuntimeHost()
    driver = CanvasRuntimeDriver(CanvasDocument(width=640, height=480))
    descriptor = await host.create(driver, runtime_id="canvas-1")
    operations = [
        CanvasOperation(
            op="upsert",
            element=CanvasElement(
                id="box",
                kind="rect",
                x=20,
                y=30,
                width=200,
                height=100,
                style=CanvasStyle(fill="#dbeafe", stroke="#2563eb"),
            ),
        ),
        CanvasOperation(
            op="upsert",
            element=CanvasElement(id="label", kind="text", x=40, y=70, text="A < B"),
        ),
    ]

    async with host.access(descriptor.ref, mode="write", owner_id="agent:test") as access:
        changed, affected = driver.apply(operations)
        access.commit(changed=changed)

    assert affected == ("box", "label")
    assert host.descriptor(descriptor.ref).revision == 1
    assert "A &lt; B" in driver.render_svg()
    checkpoint = await host.checkpoint(descriptor.ref, reason="test")
    await host.close(descriptor.ref)

    restored = CanvasRuntimeDriver()
    await host.create(restored, runtime_id="canvas-1", checkpoint=checkpoint)
    assert restored.snapshot_document().width == 640
    assert [element.id for element in restored.snapshot_document().elements] == [
        "box",
        "label",
    ]
    await host.close("canvas:default")


@pytest.mark.asyncio
async def test_canvas_recovers_prepared_wal_after_crash_before_commit(tmp_path):
    base_driver = CanvasRuntimeDriver(CanvasDocument(width=640, height=480))
    await base_driver.start()
    encoded = await base_driver.checkpoint("operation-prepare")
    await base_driver.aclose()
    base_checkpoint = RuntimeCheckpoint(
        runtime_id="canvas-wal",
        kind="canvas",
        alias="default",
        epoch=1,
        revision=0,
        codec=encoded.codec,
        schema_version=encoded.schema_version,
        payload_ref=encoded.payload_ref,
        digest=encoded.digest,
        fidelity=encoded.fidelity,
    )
    operation = CanvasOperation(
        op="upsert",
        element=CanvasElement(
            id="recovered-node",
            kind="rect",
            x=10,
            y=20,
            width=120,
            height=60,
        ),
    )
    intent = RuntimeOperationIntent(
        operation_id="canvas-wal-operation",
        runtime_id="canvas-wal",
        kind="canvas",
        alias="default",
        epoch=1,
        base_revision=0,
        target_revision=1,
        codec="canvas-operations+json@1",
        schema_version=1,
        payload=json.dumps([operation.model_dump(mode="json")]),
        base_checkpoint=base_checkpoint,
        projections=(
            RuntimeProjectionIntent(
                intent_id="artifact",
                projector="canvas-artifact",
                schema_version=1,
            ),
        ),
    )
    log = SessionLog("canvas-wal", base_dir=str(tmp_path))
    log.commit_offline(SessionMetaEvent(session_id="canvas-wal"))
    operation_journal = SessionRuntimeOperationJournal(log)
    await operation_journal.prepare(intent)
    recovered_driver = CanvasRuntimeDriver()
    artifacts = DurableArtifactStore(tmp_path / "artifacts.sqlite3", _MemoryBlobs())
    host = RuntimeHost(
        projection_journal=SessionRuntimeProjectionJournal(log),
        operation_journal=operation_journal,
        checkpoint_payload_store=ArtifactCheckpointPayloadStore(artifacts, AesGcmCipher(b"k" * 32)),
    )

    descriptor = await host.ensure(recovered_driver)

    assert descriptor.ref.runtime_id == "canvas-wal"
    assert descriptor.revision == 1
    assert [element.id for element in recovered_driver.snapshot_document().elements] == ["recovered-node"]
    recovered_log = replay(log)
    assert recovered_log.pending_runtime_operations == {}
    assert len(recovered_log.pending_runtime_projections) == 1
    recovered_projection = next(iter(recovered_log.pending_runtime_projections.values()))
    assert recovered_projection.checkpoint.payload_ref.startswith("artifact:runtime-checkpoint-")
    await host.close(descriptor.ref)


@pytest.mark.asyncio
async def test_canvas_handoff_drag_mutates_live_document():
    driver = CanvasRuntimeDriver()
    await driver.start()
    request = HandoffRequest(runtime_ref=RuntimeRef(runtime_id="canvas-2", kind="canvas"))
    handle = await driver.prepare_handoff(request)
    try:
        assert handle.surface.presentation is SurfacePresentationMode.WINDOW
        before = await driver.snapshot_surface(handle)
        await driver.send_surface_input(
            handle,
            SurfaceInput(kind="canvas.drag", data='{"x0":0.1,"y0":0.2,"x1":0.4,"y1":0.5}'),
        )
        after = await driver.snapshot_surface(handle)
        assert after.sequence == before.sequence + 1
        element = driver.snapshot_document().elements[0]
        assert element.kind == "rect"
        assert element.x == pytest.approx(120)
        assert element.width == pytest.approx(360)
        await driver.finish_handoff(handle, HumanHandoffOutcome(status=HandoffStatus.COMPLETED))
    finally:
        await driver.aclose()


@pytest.mark.asyncio
async def test_canvas_observer_survives_handoff_but_input_authority_does_not():
    driver = CanvasRuntimeDriver()
    await driver.start()
    request = HandoffRequest(runtime_ref=RuntimeRef(runtime_id="canvas-observer", kind="canvas"))
    handle = await driver.prepare_handoff(request)
    initial = await driver.snapshot_surface(handle)
    await driver.finish_handoff(handle, HumanHandoffOutcome(status=HandoffStatus.COMPLETED))

    pending = asyncio.create_task(driver.next_surface_frame(handle, initial.sequence))
    await asyncio.sleep(0)
    driver.apply(
        [
            CanvasOperation(
                op="upsert",
                element=CanvasElement(id="agent-after-handoff", kind="rect"),
            )
        ]
    )
    frame = await asyncio.wait_for(pending, timeout=1)

    assert frame is not None
    assert "agent-after-handoff" in frame.content
    with pytest.raises(RuntimeError, match="handoff handle"):
        await driver.send_surface_input(handle, SurfaceInput(kind="canvas.replace", data=frame.content))

    await driver.detach_surface(handle)
    await driver.aclose()


@pytest.mark.asyncio
async def test_canvas_handoff_coordinator_commits_human_surface_change():
    host = RuntimeHost()
    driver = CanvasRuntimeDriver()
    descriptor = await host.create(driver, runtime_id="canvas-3")

    class Interaction:
        async def open_handoff(self, request, handle, surface=None):
            assert surface is not None
            await surface.send(SurfaceInput(kind="canvas.drag", data='{"x0":0.2,"y0":0.2,"x1":0.5,"y1":0.6}'))
            return HumanHandoffOutcome(status=HandoffStatus.COMPLETED, human_message="added a box")

    outcome = await HandoffCoordinator(host, Interaction()).handoff(
        HandoffRequest(runtime_ref=descriptor.ref),
        owner_id="human:canvas-test",
    )

    assert outcome.human_message == "added a box"
    assert outcome.from_revision == 0
    assert outcome.to_revision == 1
    assert len(driver.snapshot_document().elements) == 1
    await host.close(descriptor.ref)
