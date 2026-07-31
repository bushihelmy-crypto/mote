"""Managed Runtime adapter for persistent external GUI devices."""
from __future__ import annotations

import base64
import json
from uuid import uuid4

from mote.contracts.interaction.handoff import (
    DriverHandoffHandle,
    DriverHandoffResult,
    HandoffRequest,
    HumanHandoffOutcome,
)
from mote.contracts.runtime import (
    CheckpointFidelity,
    DriverCheckpoint,
    DriverStartResult,
    RuntimeCapabilities,
    RuntimeCheckpoint,
    RuntimeHealth,
)
from mote.contracts.surface import SurfaceDescriptor, SurfaceFrame, SurfaceInput, SurfacePresentationMode
from mote.runtime.interactive.device.base import DeviceBackend
from mote.runtime.interactive.device.session import DeviceSession
from mote.runtime.interactive.observation import SurfaceObservationHub


class DeviceRuntimeDriver:
    """Own one DeviceSession behind RuntimeHost lifecycle and handoff fencing."""

    kind = "device"
    capabilities = RuntimeCapabilities(
        checkpoint_fidelity=CheckpointFidelity.NONE,
        handoff_modes=frozenset({"exclusive"}),
        surface_kinds=frozenset({"device"}),
        multi_instance=False,
    )

    def __init__(self, *, session_key: str, backend: DeviceBackend) -> None:
        self._session_key = session_key
        self._backend = backend
        self._session: DeviceSession | None = None
        self._handoff_id: str | None = None
        self._surface_sequence = 0
        self._surface_observers = SurfaceObservationHub()

    @property
    def session(self) -> DeviceSession:
        if self._session is None:
            raise RuntimeError("device runtime is not running")
        return self._session

    @property
    def closed(self) -> bool:
        return self._session is None or self._session.closed

    async def start(self, checkpoint: RuntimeCheckpoint | None = None) -> DriverStartResult:
        if self._session is not None:
            raise RuntimeError("device runtime is already started")
        if checkpoint is not None:
            raise ValueError("device runtime does not support checkpoint restore")
        session = DeviceSession(session_key=self._session_key, backend=self._backend)
        self._session = session
        try:
            await session.start()
        except BaseException:
            session.kill()
            self._session = None
            raise
        return DriverStartResult()

    async def health(self) -> RuntimeHealth:
        return RuntimeHealth(
            healthy=not self.closed,
            status="ready" if not self.closed else "closed",
        )

    async def checkpoint(self, reason: str) -> DriverCheckpoint:
        raise RuntimeError("device runtime has no restorable checkpoint")

    def surface_changed(self) -> None:
        self._advance_surface_sequence()
        self._surface_observers.notify()

    def _advance_surface_sequence(self) -> None:
        self._surface_sequence += 1

    async def prepare_handoff(self, request: HandoffRequest) -> DriverHandoffHandle:
        if self._handoff_id is not None:
            raise RuntimeError("device runtime is already handed off")
        if self.closed:
            raise RuntimeError("device runtime is not running")
        self._handoff_id = uuid4().hex
        self._surface_observers.attach(self._handoff_id)
        self._surface_observers.start_sampling(
            self._advance_surface_sequence,
            interval_seconds=0.25,
        )
        return DriverHandoffHandle(
            handle_id=self._handoff_id,
            surface=SurfaceDescriptor(
                kind="device",
                ref=f"device-session:{self._session_key}",
                presentation=SurfacePresentationMode.WINDOW,
                title="Device",
            ),
        )

    async def finish_handoff(
        self,
        handle: DriverHandoffHandle,
        outcome: HumanHandoffOutcome,
    ) -> DriverHandoffResult:
        self._assert_handoff_handle(handle)
        self._handoff_id = None
        return DriverHandoffResult(summary="Human returned control of the device runtime.")

    async def snapshot_surface(self, handle: DriverHandoffHandle) -> SurfaceFrame:
        self._assert_surface_handle(handle)
        screenshot, width, height = await self.session.capture_screen()
        payload = {
            "width": width,
            "height": height,
            "screenshot_b64": base64.b64encode(screenshot).decode("ascii"),
        }
        return SurfaceFrame(
            sequence=self._surface_sequence,
            media_type="application/vnd.mote.device+json",
            content=json.dumps(payload, separators=(",", ":")),
        )

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
        return await self.snapshot_surface(handle) if changed else None

    async def detach_surface(self, handle: DriverHandoffHandle) -> None:
        self._surface_observers.detach(handle.handle_id)

    async def send_surface_input(self, handle: DriverHandoffHandle, event: SurfaceInput) -> None:
        self._assert_handoff_handle(handle)
        if event.kind == "device.tap":
            payload = json.loads(event.data)
            await self.session.backend.tap(int(payload["x"]), int(payload["y"]))
        elif event.kind == "device.long_press":
            payload = json.loads(event.data)
            await self.session.backend.long_press(int(payload["x"]), int(payload["y"]))
        elif event.kind == "device.swipe":
            payload = json.loads(event.data)
            await self.session.backend.swipe(
                int(payload["x"]),
                int(payload["y"]),
                int(payload["x2"]),
                int(payload["y2"]),
            )
        elif event.kind == "device.text":
            await self.session.backend.input_text(event.data)
        elif event.kind == "device.key":
            await self.session.backend.key(event.data)
        else:
            raise ValueError(f"unsupported device surface input: {event.kind}")
        self.surface_changed()

    async def aclose(self) -> None:
        self._handoff_id = None
        self._surface_observers.close()
        session, self._session = self._session, None
        if session is not None:
            await session.shutdown()

    def _assert_handoff_handle(self, handle: DriverHandoffHandle) -> None:
        if handle.handle_id != self._handoff_id:
            raise RuntimeError("device handoff handle is not current")

    def _assert_surface_handle(self, handle: DriverHandoffHandle) -> None:
        if not self._surface_observers.contains(handle.handle_id):
            raise RuntimeError("device surface attachment is not current")


__all__ = ["DeviceRuntimeDriver"]
