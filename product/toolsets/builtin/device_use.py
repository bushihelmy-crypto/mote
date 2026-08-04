"""DeviceUse — drive an external GUI device (Android over adb today) via observe/act.

The device sibling of :class:`WebBrowser`: there is **one implicit device per Role
session** (no device id to track), and the model drives it by issuing actions.
Where WebBrowser owns a local chromium and reads the DOM natively, DeviceUse
reaches an *external* device and observes it screenshot-first + a11y outline —
orthogonal surfaces, deliberately not merged.

The model works an observe → act loop:

- ``observe`` — capture the screen: a screenshot (shown to the model) plus an
  indented accessibility outline whose elements carry stable ``@e{N}`` refs, all
  stamped with a ``state_id``. Pick ``mode``: ``fused`` (outline + screenshot,
  default), ``semantic`` (outline text only, cheap), ``visual`` (screenshot only,
  for surfaces the a11y layer cannot see).
- ``tap`` / ``long_press`` — act on an element by ``ref`` (``@e5`` from the latest
  observe) or a raw ``x`` / ``y`` pixel coordinate.
- ``swipe`` — drag from ``x``/``y`` to ``x2``/``y2``. ``scroll`` — scroll the screen
  in a ``direction`` (up / down / left / right).
- ``type`` — type ``text`` into the focused field (handles CJK via an IME).
  ``key`` — press a hardware / system key (BACK, HOME, ENTER, …).
- ``open_app`` — launch an app by package/label. ``list_apps`` — installed packages.
- ``wait`` — pause ``seconds`` for the UI to settle. ``close`` — release.

Refs are valid only for the snapshot that minted them: pass the observe's
``state_id`` alongside a ``ref`` and a since-moved screen fails cleanly, telling
you to observe again — you can never tap a stale element.

The live :class:`DeviceSession` is owned by the Role's ``RuntimeHost``, so each
Role's device handle is isolated, fenced, revisioned, handed off, and torn down
through the same lifecycle as Terminal, Jupyter, Browser, and Canvas.
"""

from __future__ import annotations

import asyncio
from typing import Any, ClassVar

from mote.contracts.authorization import PermissionDecision
from mote.contracts.model.capabilities import supports_vision
from mote.contracts.runtime import RuntimeAccessMode
from mote.contracts.runtime.errors import ManagedRuntimeNotFoundError
from mote.contracts.tool.effects import ToolEffect
from mote.contracts.tool.errors import ToolError, ToolNotConfiguredError
from mote.contracts.tool.result import json_tool_payload
from mote.product.toolsets.builtin.runtime_action import handoff_permission, is_handoff_action, run_handoff_action
from mote.runtime.artifacts.media import publish_media_artifact
from mote.runtime.interactive.device.backend import DeviceError, select_device_backend
from mote.runtime.interactive.device.runtime import DeviceRuntimeDriver
from mote.runtime.interactive.device.session import DeviceSession, Observation
from mote.runtime.tools.base_tool import BaseTool
from mote.runtime.tools.capability_types import (
    GetArtifactPublisher,
    GetDefaultModel,
    GetDeviceConfig,
    GetRuntimeHost,
    HandoffRuntime,
)
from mote.runtime.tools.tool_result import ToolMedia, ToolResult

# Model-facing sentences, hoisted so the wording lives in one place.
_MSG_DEVICE_FAILED = "Error running device: {error}"
_MSG_TAP_REQUIRES = (
    "Error: '{action}' requires a 'ref' (an element ref like '@e5' from the latest "
    "observe) or an 'x'/'y' pixel coordinate."
)
_MSG_SWIPE_REQUIRES = "Error: 'swipe' requires start x/y and end x2/y2 pixel coordinates."
_MSG_SCROLL_REQUIRES = "Error: 'scroll' requires a 'direction' (up | down | left | right)."
_MSG_TYPE_REQUIRES = "Error: 'type' requires 'text' to type into the focused field."
_MSG_KEY_REQUIRES = "Error: 'key' requires a 'key' name (e.g. BACK, HOME, ENTER)."
_MSG_OPEN_APP_REQUIRES = "Error: 'open_app' requires an 'app' (package name or label)."
_MSG_VISION_UNAVAILABLE = (
    "Cannot capture a screenshot: the default model '{model}' is not vision-capable, "
    "so the image would never reach it. Use observe mode='semantic' for the "
    "accessibility outline as text instead, or configure a vision model."
)
_MSG_EMPTY_OUTLINE = (
    "(no accessibility outline for this screen — act by coordinate using the "
    "screenshot: pass x/y to tap/long_press/swipe)"
)
_MSG_UNKNOWN_ACTION = (
    "Error: unknown device action '{action}'. Use observe | tap | long_press | "
    "swipe | scroll | type | key | open_app | list_apps | wait | handoff | close."
)

_SCROLL_DIRS = ("up", "down", "left", "right")
_RUNTIME = "device:default"


class DeviceUse(BaseTool):
    """Drive an external GUI device (Android over adb) — observe the screen then act."""

    name = "DeviceUse"
    aliases = ["device"]
    # Recall synonyms for tool-search: ways a model asks to drive a phone/device
    # that the one-line summary does not literally contain.
    keywords: ClassVar[list[str]] = [
        "phone",
        "device",
        "android",
        "emulator",
        "adb",
        "tap",
        "screenshot",
        "app",
        "手机",
        "设备",
        "安卓",
        "截屏",
        "点击",
        "模拟器",
    ]
    # Screenshots ride as ToolMedia (exempt from the persist/truncate layer); the
    # outline text is small, so opt out of the 50k default clamp like WebBrowser.
    max_result_size_chars: ClassVar[float] = float("inf")
    requires = (
        "get_runtime_host",
        "get_device_config",
        "get_default_model",
        "get_artifact_publisher",
        "handoff_runtime",
    )
    # Acts on an external device (taps are not idempotent) — EXTERNAL is the
    # correct effect class (the tool-level ledger guards crash-replay). Declared
    # explicitly rather than relying on resolve_effect's default for clarity.
    effect: ClassVar[ToolEffect] = ToolEffect.EXTERNAL
    # Navigates and mutates an external device.
    risk_level = "high"
    # Fronts one live device managed by RuntimeHost between calls.
    stateful = True

    get_runtime_host: GetRuntimeHost
    get_artifact_publisher: GetArtifactPublisher
    handoff_runtime: HandoffRuntime
    # Device backend selection config (backend / adb_path / serial). Defaults to
    # a stub so a tool bound without a Role (unit tests) can still construct.
    get_device_config: GetDeviceConfig = staticmethod(lambda: None)  # type: ignore[assignment,return-value]
    # Default (main think-loop) model name: a screenshot rides the MAIN model's
    # request, so observe checks ``supports_vision`` up-front. Defaults to a None
    # stub so a tool bound without a Role (unit tests) screenshots normally.
    get_default_model: GetDefaultModel = staticmethod(lambda: None)

    async def _ensure_runtime(self) -> None:
        """Atomically connect this Role's implicit managed device Runtime."""
        host = self.get_runtime_host()
        try:
            host.descriptor(_RUNTIME)
            return
        except ManagedRuntimeNotFoundError:
            pass
        backend = select_device_backend(self.get_device_config())
        await host.ensure(DeviceRuntimeDriver(session_key=self.session_id, backend=backend))

    async def call(
        self,
        *,
        action: str = "observe",
        mode: str = "fused",
        ref: str = "",
        state_id: str = "",
        x: int = -1,
        y: int = -1,
        x2: int = -1,
        y2: int = -1,
        direction: str = "",
        text: str = "",
        key: str = "",
        app: str = "",
        seconds: float = 1.0,
        message: str = "",
    ) -> Any:
        """Drive a persistent external device — observe the screen, then tap/type/swipe.

        One device per session. Work an observe → act loop: ``observe`` to see the
        screen (screenshot + an accessibility outline whose elements carry stable
        ``@e{N}`` refs, stamped with a ``state_id``), then act by ref or coordinate.
        Actions:

        - observe — mode=fused (outline + screenshot, default), semantic (outline
          only, cheap), visual (screenshot only). Re-observe after any screen
          change — refs are only valid for the latest snapshot.
        - tap / long_press — pass ref (like '@e5') OR raw x/y pixels. Pass the
          observe's state_id with a ref so a since-moved screen fails cleanly.
        - swipe — drag x/y → x2/y2. scroll — direction (up|down|left|right).
        - type text into the focused field (handles CJK). key — a system key
          (BACK, HOME, ENTER, RECENT, …).
        - open_app by package/label. list_apps — installed packages.
        - wait — pause seconds. handoff — give the user exclusive control of the
          already-open device and wait for its return. close — release.

        When the a11y outline is empty (game/custom-drawn/secured surface), observe
        still returns the screenshot: read it and act by x/y coordinate.

        Args:
            action: One of observe | tap | long_press | swipe | scroll | type |
                key | open_app | list_apps | wait | handoff | close.
            mode: For observe — fused (default) | semantic | visual.
            ref: For tap / long_press — an element ref from the latest observe
                (like '@e5').
            state_id: The state_id from the observe that minted ``ref`` — pass it so
                a stale ref (screen moved) is rejected with a clear re-observe hint.
            x: For tap / long_press / swipe — the (start) x pixel coordinate.
            y: For tap / long_press / swipe — the (start) y pixel coordinate.
            x2: For swipe — the end x pixel coordinate.
            y2: For swipe — the end y pixel coordinate.
            direction: For scroll — up | down | left | right.
            text: For type — the text to type into the focused field.
            key: For key — the key name (e.g. BACK, HOME, ENTER, RECENT).
            app: For open_app — the app package name or label to launch.
            seconds: For wait — how long to pause (default 1.0).
            message: Optional instructions shown to the user during handoff.
        """
        action = (action or "").strip().lower()

        if action == "handoff":
            return await run_handoff_action(self.handoff_runtime, _RUNTIME, message=message)
        if action == "close":
            host = self.get_runtime_host()
            try:
                host.descriptor(_RUNTIME)
            except ManagedRuntimeNotFoundError:
                return "[no device to close]"
            await host.close(_RUNTIME)
            return "[device closed]"

        if action == "wait":
            await asyncio.sleep(max(0.0, float(seconds)))
            return f"[waited {seconds}s]"

        try:
            await self._ensure_runtime()
            host = self.get_runtime_host()
            async with host.access(
                _RUNTIME,
                mode=RuntimeAccessMode.WRITE,
                owner_id=f"agent:{self.session_id}:device",
            ) as access:
                driver = access.driver
                if not isinstance(driver, DeviceRuntimeDriver):
                    raise RuntimeError("device runtime has an unexpected driver")
                result = await self._dispatch(
                    driver.session,
                    action,
                    mode=mode,
                    ref=ref,
                    state_id=state_id,
                    x=x,
                    y=y,
                    x2=x2,
                    y2=y2,
                    direction=direction,
                    text=text,
                    key=key,
                    app=app,
                )
                changed = action != "list_apps"
                if changed:
                    driver.surface_changed()
                access.commit(changed=changed)
        except (ToolError, ToolNotConfiguredError):
            raise
        except DeviceError as e:
            raise ToolError(_MSG_DEVICE_FAILED.format(error=e))
        except Exception as e:  # noqa: BLE001
            raise ToolError(_MSG_DEVICE_FAILED.format(error=e))
        return result

    def check_permissions(self, args: dict) -> PermissionDecision | None:
        if is_handoff_action(args):
            return handoff_permission()
        return None

    async def cleanup_session(self, session_id: str) -> None:
        host = self.get_runtime_host()
        try:
            host.descriptor(_RUNTIME)
        except ManagedRuntimeNotFoundError:
            return
        await host.close(_RUNTIME)

    async def _dispatch(
        self,
        session: DeviceSession,
        action: str,
        *,
        mode: str,
        ref: str,
        state_id: str,
        x: int,
        y: int,
        x2: int,
        y2: int,
        direction: str,
        text: str,
        key: str,
        app: str,
    ) -> Any:
        """Route *action* to the matching device operation, returning its result."""
        if action == "observe":
            return await self._observe(session, mode=mode)
        if action == "tap":
            px, py = self._point(session, action, ref, state_id, x, y)
            await session.backend.tap(px, py)
            return f"[tapped ({px}, {py})]"
        if action == "long_press":
            px, py = self._point(session, action, ref, state_id, x, y)
            await session.backend.long_press(px, py)
            return f"[long-pressed ({px}, {py})]"
        if action == "swipe":
            if x < 0 or y < 0 or x2 < 0 or y2 < 0:
                raise ToolError(_MSG_SWIPE_REQUIRES)
            await session.backend.swipe(x, y, x2, y2)
            return f"[swiped ({x}, {y}) -> ({x2}, {y2})]"
        if action == "scroll":
            return await self._scroll(session, direction)
        if action == "type":
            if not text:
                raise ToolError(_MSG_TYPE_REQUIRES)
            await session.backend.input_text(text)
            return "[typed text]"
        if action == "key":
            if not key:
                raise ToolError(_MSG_KEY_REQUIRES)
            await session.backend.key(key)
            return f"[pressed key {key}]"
        if action == "open_app":
            if not app:
                raise ToolError(_MSG_OPEN_APP_REQUIRES)
            await session.backend.open_app(app)
            return f"[opened app {app}]"
        if action == "list_apps":
            apps = await session.backend.list_apps()
            return "\n".join(apps) if apps else "[no apps found]"
        raise ToolError(_MSG_UNKNOWN_ACTION.format(action=action))

    async def _observe(self, session: DeviceSession, *, mode: str) -> ToolResult:
        """Capture an observation and render it (outline text + screenshot media)."""
        want_shot = mode != "semantic"
        vision_ok = True
        if want_shot:
            model = self.get_default_model() if self.get_default_model is not None else None
            vision_ok = model is None or supports_vision(model)
            if mode == "visual" and not vision_ok:
                raise ToolNotConfiguredError(_MSG_VISION_UNAVAILABLE.format(model=model))
        obs = await session.observe(mode=mode)
        if obs.screenshot is not None and not vision_ok:
            # Model can't see images — drop the screenshot, keep the outline.
            obs.screenshot = None
        return await self._render_observation(obs)

    async def _render_observation(self, obs: Observation) -> ToolResult:
        """Build the ToolResult for an observation (screenshot → ToolMedia)."""
        lines = [f"[observation {obs.state_id}  screen {obs.width}x{obs.height}]"]
        if obs.empty:
            lines.append(_MSG_EMPTY_OUTLINE)
        if obs.text:
            lines.append(obs.text)
        media: list[ToolMedia] = []
        if obs.screenshot is not None:
            artifact = await publish_media_artifact(
                self.get_artifact_publisher(),
                content=obs.screenshot,
                representation="png",
                kind="device-screenshot",
                mime_type="image/png",
                suggested_name=f"device-{obs.state_id}.png",
            )
            media.append(ToolMedia(kind="image", mime="image/png", artifact=artifact))
        return ToolResult(
            output="\n".join(lines),
            media=tuple(media),
            payload=json_tool_payload({"state_id": obs.state_id, "empty": obs.empty}),
        )

    def _point(
        self,
        session: DeviceSession,
        action: str,
        ref: str,
        state_id: str,
        x: int,
        y: int,
    ) -> tuple[int, int]:
        """Resolve a tap point from a ``@e{N}`` ref (preferred) or raw x/y pixels."""
        if ref:
            return session.resolve_ref(ref, state_id=state_id or None)
        if x >= 0 and y >= 0:
            return (x, y)
        raise ToolError(_MSG_TAP_REQUIRES.format(action=action))

    async def _scroll(self, session: DeviceSession, direction: str) -> str:
        """Scroll the screen by swiping across its center in *direction*."""
        direction = (direction or "").strip().lower()
        if direction not in _SCROLL_DIRS:
            raise ToolError(_MSG_SCROLL_REQUIRES)
        w, h = await session.backend.screen_size()
        cx, cy = w // 2, h // 2
        dx, dy = w // 3, h // 3
        # direction = the direction to move the viewport; the finger swipes the
        # opposite way (drag content toward the requested edge).
        vectors = {
            "down": (cx, cy + dy, cx, cy - dy),
            "up": (cx, cy - dy, cx, cy + dy),
            "right": (cx + dx, cy, cx - dx, cy),
            "left": (cx - dx, cy, cx + dx, cy),
        }
        x1, y1, x2, y2 = vectors[direction]
        await session.backend.swipe(x1, y1, x2, y2)
        return f"[scrolled {direction}]"


__all__ = ["DeviceUse"]
