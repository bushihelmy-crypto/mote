"""Managed Runtime adapter for the persistent Playwright browser session."""

from __future__ import annotations

import base64
import json
from typing import Any, Dict, List, Optional
from uuid import uuid4

from mote.contracts.browser import BrowserProfileSnapshot
from mote.contracts.events.envelope import thaw_json
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
from mote.runtime.interactive.browser.session import BrowserSession
from mote.runtime.interactive.checkpoint_codec import BROWSER_CHECKPOINT_CODEC, BrowserCheckpointState
from mote.runtime.interactive.observation import SurfaceObservationHub


class BrowserRuntimeDriver:
    """Own one BrowserSession behind RuntimeHost lifecycle and handoff fencing."""

    kind = "browser"
    capabilities = RuntimeCapabilities(
        checkpoint_fidelity=CheckpointFidelity.LOGICAL,
        handoff_modes=frozenset({"exclusive"}),
        surface_kinds=frozenset({"browser"}),
        multi_instance=False,
    )

    def __init__(
        self,
        *,
        session_key: str,
        cwd: Optional[str] = None,
        stealth: bool = False,
        browser_locale: str = "en",
        proxy: str = "",
        cdp_endpoint: str = "",
        client_certs: Optional[List[Dict[str, Any]]] = None,
        storage_state: dict[str, Any] | None = None,
        persist_storage_state: bool = True,
        profile_snapshot: BrowserProfileSnapshot | None = None,
    ) -> None:
        self._session_kwargs = {
            "session_key": session_key,
            "cwd": cwd,
            "headless": True,
            "stealth": stealth,
            "browser_locale": browser_locale,
            "proxy": proxy,
            "cdp_endpoint": cdp_endpoint,
            "client_certs": client_certs,
        }
        self._storage_state = storage_state
        self._persist_storage_state = persist_storage_state
        self.profile_snapshot = profile_snapshot
        self._session: BrowserSession | None = None
        self._handoff_id: str | None = None
        self._surface_sequence = 0
        self._surface_observers = SurfaceObservationHub()

    @property
    def session(self) -> BrowserSession:
        if self._session is None:
            raise RuntimeError("browser runtime is not running")
        return self._session

    @property
    def closed(self) -> bool:
        return self._session is None or self._session.closed

    async def start(self, checkpoint: RuntimeCheckpoint | None = None) -> DriverStartResult:
        if self._session is not None:
            raise RuntimeError("browser runtime is already started")
        restore = None
        if checkpoint is not None:
            restore = BROWSER_CHECKPOINT_CODEC.decode(checkpoint)
        storage_state = self._storage_state
        if storage_state is None and restore is not None:
            candidate = restore.storage_state
            thawed = thaw_json(dict(candidate)) if candidate is not None else None
            storage_state = thawed if isinstance(thawed, dict) else None
        session = BrowserSession(**self._session_kwargs)
        self._session = session
        try:
            await session.start(storage_state=storage_state)
            if restore is not None:
                await session.restore_state(
                    list(restore.urls),
                    restore.active,
                    storage_state,
                )
        except BaseException:
            session.kill()
            self._session = None
            raise
        return DriverStartResult(restored=restore is not None)

    async def health(self) -> RuntimeHealth:
        return RuntimeHealth(healthy=not self.closed, status="ready" if not self.closed else "closed")

    async def checkpoint(self, reason: str) -> DriverCheckpoint:
        state = await self.session.capture_state()
        if state is None:
            raise RuntimeError("browser logical state is unavailable")
        urls, active, storage_state = state
        return BROWSER_CHECKPOINT_CODEC.encode(
            BrowserCheckpointState(
                tuple(urls),
                active,
                storage_state if self._persist_storage_state else None,
            ),
            fidelity=CheckpointFidelity.LOGICAL,
            sensitivity="secret",
        )

    def surface_changed(self) -> None:
        self._advance_surface_sequence()
        self._surface_observers.notify()

    def _advance_surface_sequence(self) -> None:
        self._surface_sequence += 1

    async def prepare_handoff(self, request: HandoffRequest) -> DriverHandoffHandle:
        if self._handoff_id is not None:
            raise RuntimeError("browser runtime is already handed off")
        if self.closed:
            raise RuntimeError("browser runtime is not running")
        await self._promote_to_headed_window()
        self._handoff_id = uuid4().hex
        self._surface_observers.attach(self._handoff_id)
        self._surface_observers.start_sampling(
            self._advance_surface_sequence,
            interval_seconds=0.25,
        )
        return DriverHandoffHandle(
            handle_id=self._handoff_id,
            surface=SurfaceDescriptor(
                kind="browser",
                ref=f"browser-session:{self._session_kwargs['session_key']}",
                presentation=SurfacePresentationMode.WINDOW,
                title="Browser",
            ),
        )

    async def _promote_to_headed_window(self) -> None:
        """Move a handoff from the screenshot proxy to its real Chromium page.

        A headless page rendered into a second Chromium window is adequate for
        observation, but it is not a faithful human browser: responsive layout,
        device scaling, and bot challenges can all diverge.  Before handing
        ownership to a person, restart once as a headed browser using the same
        URLs and storage state, then focus that real page.
        """
        session = self.session
        if not session.headless:
            await session.focus()
            return
        state = await session.capture_state()
        if state is None:
            raise RuntimeError("browser state is unavailable for human handoff")
        urls, active, storage_state = state
        await session.shutdown()
        self._session_kwargs["headless"] = False
        headed = BrowserSession(**self._session_kwargs)
        self._session = headed
        try:
            await headed.start(storage_state=storage_state)
            await headed.restore_state(urls, active, storage_state)
            await headed.focus()
        except BaseException:
            await headed.shutdown()
            self._session = None
            raise

    async def finish_handoff(
        self,
        handle: DriverHandoffHandle,
        outcome: HumanHandoffOutcome,
    ) -> DriverHandoffResult:
        self._assert_handoff_handle(handle)
        self._handoff_id = None
        return DriverHandoffResult(summary="Human returned control of the browser runtime.")

    async def snapshot_surface(self, handle: DriverHandoffHandle) -> SurfaceFrame:
        self._assert_surface_handle(handle)
        payload = {
            "tabs": await self.session.tabs(),
            "screenshot_b64": base64.b64encode(await self.session.screenshot()).decode("ascii"),
        }
        return SurfaceFrame(
            sequence=self._surface_sequence,
            media_type="application/vnd.mote.browser+json",
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
        if event.kind == "browser.pointer":
            payload = json.loads(event.data)
            await self.session.handoff_pointer(float(payload["x"]), float(payload["y"]))
        elif event.kind == "browser.drag":
            payload = json.loads(event.data)
            await self.session.handoff_drag(
                float(payload["x"]),
                float(payload["y"]),
                float(payload["x2"]),
                float(payload["y2"]),
            )
        elif event.kind == "browser.text":
            await self.session.handoff_text(event.data)
        elif event.kind == "browser.key":
            await self.session.handoff_key(event.data)
        elif event.kind == "browser.back":
            await self.session.back()
        else:
            raise ValueError(f"unsupported browser surface input: {event.kind}")
        self.surface_changed()

    async def aclose(self) -> None:
        self._handoff_id = None
        self._surface_observers.close()
        session, self._session = self._session, None
        if session is not None:
            await session.shutdown()

    def _assert_handoff_handle(self, handle: DriverHandoffHandle) -> None:
        if handle.handle_id != self._handoff_id:
            raise RuntimeError("browser handoff handle is not current")

    def _assert_surface_handle(self, handle: DriverHandoffHandle) -> None:
        if not self._surface_observers.contains(handle.handle_id):
            raise RuntimeError("browser surface attachment is not current")


__all__ = ["BrowserRuntimeDriver"]
