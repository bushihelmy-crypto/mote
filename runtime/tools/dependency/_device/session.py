"""DeviceSession — the backend-agnostic, per-Role live device handle.

Mirrors :class:`mote.runtime.tools.dependency._browser.BrowserSession`'s lifecycle
(start / closed / shutdown / kill) but for a "device" driven by a pluggable
:class:`DeviceBackend`. It owns:

* the backend instance (the mechanism — adb today);
* the latest :class:`Snapshot` (stable ``@e{N}`` refs anchored to a ``state_id``);
* an :class:`asyncio.Lock` serializing device access, so parallel run-graph
  branches sharing one device never interleave a screenshot with a tap.

``observe`` produces a new snapshot (screenshot + a11y outline, per ``mode``);
``resolve_ref`` turns a ``@e{N}`` ref back into a pixel tap point, raising a clear
:class:`DeviceError` when the ref is stale (the screen moved since the snapshot
that minted it) so the tool tells the model to re-observe — structurally
preventing a tap on a since-moved element.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Optional

from mote.runtime.tools.dependency._device.backend import DeviceBackend, DeviceError
from mote.runtime.tools.dependency._device.outline import Snapshot, build_snapshot, render_snapshot

# Observation modes (cost/fidelity trade-off, mirrors pi-computer-use readText):
#   fused    — outline text + screenshot (default; a GUI agent wants to see it)
#   semantic — outline text only (cheap; when a11y is enough, save the image tokens)
#   visual   — screenshot only (a11y empty/untrusted → pure-visual grounding)
_MODES = ("fused", "semantic", "visual")


@dataclass
class Observation:
    """The result of :meth:`DeviceSession.observe` — what the tool returns.

    ``state_id`` stamps the snapshot the refs belong to; ``text`` is the rendered
    a11y outline (empty on visual-only / empty surface); ``screenshot`` is the PNG
    bytes (None on semantic-only); ``empty`` flags an a11y-blind surface so the
    tool can nudge the model toward coordinate grounding.
    """

    state_id: str
    text: str
    screenshot: Optional[bytes]
    width: int
    height: int
    empty: bool


class DeviceSession:
    """A persistent device handle owned by a Role session (keyed by tool name)."""

    def __init__(self, *, session_key: str, backend: DeviceBackend) -> None:
        self.session_key = session_key
        self._backend = backend
        self._snapshot: Optional[Snapshot] = None
        self._counter = 0
        self._closed = False
        # Serializes device access so concurrent branches never interleave.
        self._lock = asyncio.Lock()

    @property
    def backend(self) -> DeviceBackend:
        return self._backend

    @property
    def snapshot(self) -> Optional[Snapshot]:
        return self._snapshot

    # --- lifecycle ---------------------------------------------------------

    async def start(self) -> None:
        """Connect to the device (idempotent); raises DeviceError if unreachable."""
        await self._backend.start()

    @property
    def closed(self) -> bool:
        return self._closed

    async def shutdown(self) -> None:
        """Graceful async teardown — release the backend connection."""
        self._closed = True
        try:
            await self._backend.shutdown()
        except Exception:  # noqa: BLE001 - teardown is best-effort
            pass

    def kill(self) -> None:
        """Best-effort synchronous teardown (idempotent) — for cleanup_session.

        adb owns no persistent per-session process (each command is its own
        short-lived subprocess), so there is nothing to SIGKILL — marking the
        session closed is all cleanup needs. The async ``shutdown`` is the
        graceful path when an event loop is available.
        """
        self._closed = True

    # --- observation -------------------------------------------------------

    def _next_state_id(self) -> str:
        self._counter += 1
        return f"s{self._counter}"

    async def observe(self, *, mode: str = "fused") -> Observation:
        """Capture a fresh observation and mint a new snapshot of ``@e{N}`` refs.

        ``mode`` picks the fidelity: ``fused`` (outline + screenshot, default),
        ``semantic`` (outline only), ``visual`` (screenshot only). The screenshot
        is always available as the robust floor; the outline accelerates ref-based
        acting when the a11y layer can see the surface.
        """
        if mode not in _MODES:
            raise DeviceError(f"unknown observe mode {mode!r}; use one of {', '.join(_MODES)}")
        async with self._lock:
            png: Optional[bytes] = None
            if mode != "semantic":
                png = await self._backend.screenshot()
            state_id = self._next_state_id()
            if mode == "visual":
                # No a11y outline requested — but keep screen size for grounding.
                width, height = await self._backend.screen_size()
                snap = Snapshot(state_id=state_id, width=width, height=height)
                self._snapshot = snap
                return Observation(state_id=state_id, text="", screenshot=png, width=width, height=height, empty=True)
            raw = await self._backend.dump_outline()
            if not (raw.width and raw.height):
                raw.width, raw.height = await self._backend.screen_size()
            snap = build_snapshot(raw, state_id=state_id, prev=self._snapshot)
            self._snapshot = snap
            text = render_snapshot(snap)
            return Observation(
                state_id=state_id,
                text=text,
                screenshot=png,
                width=snap.width,
                height=snap.height,
                empty=snap.empty,
            )

    async def capture_screen(self) -> tuple[bytes, int, int]:
        """Capture a visual Surface frame without mutating semantic ref state."""
        async with self._lock:
            screenshot = await self._backend.screenshot()
            width, height = await self._backend.screen_size()
            return screenshot, width, height

    def resolve_ref(self, ref: str, *, state_id: Optional[str] = None) -> tuple[int, int]:
        """Resolve a ``@e{N}`` ref to a pixel tap point against the latest snapshot.

        Raises :class:`DeviceError` when there is no snapshot yet, when *state_id*
        (if the model supplied one) does not match the current snapshot (the screen
        moved — re-observe), or when the ref is unknown in the current snapshot.
        """
        snap = self._snapshot
        if snap is None:
            raise DeviceError("no snapshot yet — call observe first to get element refs.")
        if state_id is not None and state_id != snap.state_id:
            raise DeviceError(
                f"stale element ref: {ref} was from snapshot {state_id!r} but the current "
                f"snapshot is {snap.state_id!r}. Call observe again to get fresh refs."
            )
        point = snap.center_of(ref)
        if point is None:
            raise DeviceError(
                f"unknown element ref {ref} in snapshot {snap.state_id!r}. "
                "Call observe again to get current element refs."
            )
        return point


__all__ = ["DeviceSession", "Observation"]
