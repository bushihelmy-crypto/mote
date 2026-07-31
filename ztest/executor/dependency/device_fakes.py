"""Shared DeviceUse test double — a scriptable in-memory :class:`DeviceBackend`.

Importable from both the dependency tests (session) and the tools tests
(device_use), since no conftest is shared across those two directories. Records
every action call as an ``(op, args)`` tuple so tests can assert what the session
/ tool dispatched, and returns fixed PNG bytes + a fixed uiautomator XML so an
observe() produces a deterministic snapshot with no real device.
"""
from __future__ import annotations

from mote.runtime.interactive.device.backend import DeviceBackend, DeviceError
from mote.runtime.interactive.device.outline import RawOutline, parse_uiautomator_xml

# A minimal 1x1 PNG (valid header) — enough to prove screenshot bytes flow.
FAKE_PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01"
    b"\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
)

# A small representative uiautomator dump: a button + edit field under a frame.
FAKE_XML = """<?xml version='1.0' encoding='UTF-8' standalone='yes' ?>
<hierarchy rotation="0">
  <node index="0" text="" class="android.widget.FrameLayout" package="com.x"
        content-desc="" clickable="false" enabled="true" scrollable="false"
        long-clickable="false" bounds="[0,0][1080,2340]">
    <node index="0" text="Home" class="android.widget.TextView"
          clickable="false" enabled="true" bounds="[40,120][400,180]" />
    <node index="1" text="" content-desc="Search" class="android.widget.Button"
          clickable="true" enabled="true" bounds="[900,120][1040,180]" />
    <node index="2" text="" class="android.widget.EditText" clickable="true"
          enabled="true" bounds="[40,200][1040,280]" />
  </node>
</hierarchy>"""


class FakeDeviceBackend(DeviceBackend):
    """A DeviceBackend that fakes a device entirely in memory.

    * ``screenshot()`` returns :data:`FAKE_PNG`.
    * ``dump_outline()`` returns the outline parsed from ``xml`` (default
      :data:`FAKE_XML`; set to ``""`` to simulate an empty a11y surface).
    * every action appends ``(op, args...)`` to :attr:`calls`.
    """

    name = "fake"

    def __init__(self, *, xml: str = FAKE_XML, width: int = 1080, height: int = 2340) -> None:
        self._xml = xml
        self._width = width
        self._height = height
        self.calls: list[tuple] = []
        self.started = False
        self.shut = False
        self._apps = ["com.android.settings", "com.example.app"]

    @property
    def available(self) -> bool:
        return True

    async def start(self) -> None:
        self.started = True

    async def shutdown(self) -> None:
        self.shut = True

    async def screenshot(self) -> bytes:
        return FAKE_PNG

    async def dump_outline(self) -> RawOutline:
        return parse_uiautomator_xml(self._xml)

    async def screen_size(self) -> tuple[int, int]:
        return (self._width, self._height)

    async def tap(self, x: int, y: int) -> None:
        self.calls.append(("tap", x, y))

    async def long_press(self, x: int, y: int, *, duration_ms: int = 800) -> None:
        self.calls.append(("long_press", x, y, duration_ms))

    async def swipe(self, x1: int, y1: int, x2: int, y2: int, *, duration_ms: int = 300) -> None:
        self.calls.append(("swipe", x1, y1, x2, y2, duration_ms))

    async def input_text(self, text: str) -> None:
        self.calls.append(("input_text", text))

    async def key(self, keycode: str) -> None:
        self.calls.append(("key", keycode))

    async def open_app(self, app: str) -> None:
        self.calls.append(("open_app", app))

    async def list_apps(self) -> list[str]:
        self.calls.append(("list_apps",))
        return list(self._apps)


class RaisingDeviceBackend(FakeDeviceBackend):
    """A backend whose actions raise :class:`DeviceError` (drives error paths)."""

    name = "raising"

    async def screenshot(self) -> bytes:
        raise DeviceError("boom")

    async def tap(self, x: int, y: int) -> None:
        raise DeviceError("no device")


__all__ = ["FakeDeviceBackend", "RaisingDeviceBackend", "FAKE_PNG", "FAKE_XML"]
