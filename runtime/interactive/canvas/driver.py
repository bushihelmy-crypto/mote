"""Managed vector Canvas driver with atomic operations and live snapshots."""

from __future__ import annotations

import json
from uuid import uuid4

from mote.contracts.interaction.handoff import (
    DriverHandoffHandle,
    DriverHandoffResult,
    HandoffRequest,
    HumanHandoffOutcome,
)
from mote.contracts.ports.surface.canvas_backend import CanvasExportPort
from mote.contracts.runtime import (
    CheckpointFidelity,
    DriverCheckpoint,
    DriverStartResult,
    RuntimeCapabilities,
    RuntimeCheckpoint,
    RuntimeHealth,
    RuntimeOperationIntent,
)
from mote.contracts.surface import (
    CanvasDocument,
    CanvasElement,
    CanvasExportRepresentation,
    CanvasOperation,
    CanvasStyle,
    SurfaceDescriptor,
    SurfaceFrame,
    SurfaceInput,
    SurfacePresentationMode,
)
from mote.runtime.interactive.canvas.export import CanvasExportService
from mote.runtime.interactive.canvas.state import apply_canvas_operations
from mote.runtime.interactive.canvas.svg import render_canvas_svg
from mote.runtime.interactive.checkpoint_codec import CANVAS_CHECKPOINT_CODEC
from mote.runtime.interactive.observation import SurfaceObservationHub


class CanvasRuntimeDriver:
    """In-process vector document owned by the shared managed Runtime host."""

    kind = "canvas"
    capabilities = RuntimeCapabilities(
        checkpoint_fidelity=CheckpointFidelity.FULL,
        handoff_modes=frozenset({"exclusive"}),
        surface_kinds=frozenset({"canvas"}),
        multi_instance=True,
    )

    def __init__(
        self,
        document: CanvasDocument | None = None,
        *,
        export_service: CanvasExportPort | None = None,
    ) -> None:
        self._document = document.model_copy(deep=True) if document is not None else CanvasDocument()
        self._started = False
        self._closed = False
        self._surface_sequence = 0
        self._handoff_id: str | None = None
        self._surface_observers = SurfaceObservationHub()
        self._export_service = export_service or CanvasExportService()

    async def start(self, checkpoint: RuntimeCheckpoint | None = None) -> DriverStartResult:
        if self._started and not self._closed:
            raise RuntimeError("canvas runtime is already started")
        if checkpoint is not None:
            self._document = CANVAS_CHECKPOINT_CODEC.decode(checkpoint)
        self._started = True
        self._closed = False
        return DriverStartResult(restored=checkpoint is not None)

    async def health(self) -> RuntimeHealth:
        return RuntimeHealth(
            healthy=self._started and not self._closed,
            status="ready" if self._started and not self._closed else "closed",
        )

    async def checkpoint(self, reason: str) -> DriverCheckpoint:
        self._assert_open()
        return CANVAS_CHECKPOINT_CODEC.encode(
            self._document,
            fidelity=CheckpointFidelity.FULL,
        )

    def apply(self, operations: list[CanvasOperation]) -> tuple[bool, tuple[str, ...]]:
        """Validate and atomically apply an ordered operation batch."""
        self._assert_open()
        self._document, changed, affected = apply_canvas_operations(self._document, operations)
        if changed:
            self._surface_sequence += 1
            self._notify_surface_observers()
        return changed, affected

    def snapshot_document(self) -> CanvasDocument:
        self._assert_open()
        return self._document.model_copy(deep=True)

    def render_svg(self) -> str:
        return render_canvas_svg(self.snapshot_document())

    async def export_representations(
        self,
        document: CanvasDocument,
        formats: tuple[str, ...] = ("svg",),
    ) -> tuple[CanvasExportRepresentation, ...]:
        """Export an immutable snapshot without consulting mutable Runtime state."""
        return await self._export_service.export(document, formats)

    async def replay_operation(self, intent: RuntimeOperationIntent) -> None:
        """Replay one WAL batch against its recorded base checkpoint."""
        if intent.codec != "canvas-operations+json@1" or intent.schema_version != 1:
            raise ValueError("unsupported canvas operation journal codec")
        payload = json.loads(intent.payload)
        if not isinstance(payload, list):
            raise ValueError("canvas operation journal payload must be a list")
        operations = [CanvasOperation.model_validate(item) for item in payload]
        self.apply(operations)

    async def prepare_handoff(self, request: HandoffRequest) -> DriverHandoffHandle:
        self._assert_open()
        if self._handoff_id is not None:
            raise RuntimeError("canvas runtime is already handed off")
        self._handoff_id = uuid4().hex
        self._surface_observers.attach(self._handoff_id)
        return DriverHandoffHandle(
            handle_id=self._handoff_id,
            surface=SurfaceDescriptor(
                kind="canvas",
                ref=f"canvas-document:{request.runtime_ref.runtime_id}",
                presentation=SurfacePresentationMode.WINDOW,
                title="Canvas",
            ),
        )

    async def finish_handoff(
        self,
        handle: DriverHandoffHandle,
        outcome: HumanHandoffOutcome,
    ) -> DriverHandoffResult:
        self._assert_handoff_handle(handle)
        self._handoff_id = None
        return DriverHandoffResult(
            summary=f"Canvas returned with {len(self._document.elements)} elements.",
            resume_hint="Observe the current canvas before applying follow-up edits.",
        )

    async def snapshot_surface(self, handle: DriverHandoffHandle) -> SurfaceFrame:
        self._assert_surface_handle(handle)
        return self._surface_frame()

    async def next_surface_frame(
        self,
        handle: DriverHandoffHandle,
        after_sequence: int,
    ) -> SurfaceFrame | None:
        self._assert_surface_handle(handle)
        changed = await self._surface_observers.wait_for_change(
            handle.handle_id,
            after_sequence,
            lambda: self._surface_sequence,
        )
        return self._surface_frame() if changed else None

    async def detach_surface(self, handle: DriverHandoffHandle) -> None:
        self._surface_observers.detach(handle.handle_id)

    def _surface_frame(self) -> SurfaceFrame:
        return SurfaceFrame(
            sequence=self._surface_sequence,
            media_type="application/vnd.mote.canvas+json",
            content=self._document.model_dump_json(),
        )

    async def send_surface_input(self, handle: DriverHandoffHandle, event: SurfaceInput) -> None:
        self._assert_handoff_handle(handle)
        if event.kind == "canvas.operations":
            raw = json.loads(event.data)
            self.apply([CanvasOperation.model_validate(item) for item in raw])
            return
        if event.kind == "canvas.replace":
            document = CanvasDocument.model_validate_json(event.data)
            if document != self._document:
                self._document = document.model_copy(deep=True)
                self._surface_sequence += 1
                self._notify_surface_observers()
            return
        if event.kind == "canvas.drag":
            drag = json.loads(event.data)
            x0 = min(max(float(drag["x0"]), 0.0), 1.0) * self._document.width
            y0 = min(max(float(drag["y0"]), 0.0), 1.0) * self._document.height
            x1 = min(max(float(drag["x1"]), 0.0), 1.0) * self._document.width
            y1 = min(max(float(drag["y1"]), 0.0), 1.0) * self._document.height
            element = CanvasElement(
                id=f"human-{uuid4().hex[:10]}",
                kind="rect",
                x=min(x0, x1),
                y=min(y0, y1),
                width=max(abs(x1 - x0), 4.0),
                height=max(abs(y1 - y0), 4.0),
                style=CanvasStyle(fill="#7aa2f733", stroke="#7aa2f7"),
            )
            self.apply([CanvasOperation(op="upsert", element=element)])
            return
        raise ValueError(f"unsupported canvas surface input: {event.kind}")

    async def aclose(self) -> None:
        self._handoff_id = None
        self._closed = True
        self._surface_observers.close()

    def _assert_open(self) -> None:
        if not self._started or self._closed:
            raise RuntimeError("canvas runtime is not running")

    def _assert_handoff_handle(self, handle: DriverHandoffHandle) -> None:
        if handle.handle_id != self._handoff_id:
            raise RuntimeError("canvas handoff handle is not current")

    def _assert_surface_handle(self, handle: DriverHandoffHandle) -> None:
        if not self._surface_observers.contains(handle.handle_id):
            raise RuntimeError("canvas surface attachment is not current")

    def _notify_surface_observers(self) -> None:
        self._surface_observers.notify()


__all__ = ["CanvasRuntimeDriver"]
