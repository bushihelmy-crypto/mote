"""Device-driver contract shared by backend selection and implementations."""
from __future__ import annotations

from mote.runtime.tools.dependency._device.outline import RawOutline


class DeviceError(RuntimeError):
    """A device operation failed or no configured device is available."""


class DeviceBackend:
    """Abstract strategy for observing and acting on one device."""

    name = "base"

    @property
    def available(self) -> bool:
        return False

    async def start(self) -> None:
        raise NotImplementedError

    async def shutdown(self) -> None:
        """Release the device connection (idempotent, best-effort)."""

    async def screenshot(self) -> bytes:
        raise NotImplementedError

    async def dump_outline(self) -> RawOutline:
        raise NotImplementedError

    async def screen_size(self) -> tuple[int, int]:
        raise NotImplementedError

    async def tap(self, x: int, y: int) -> None:
        raise NotImplementedError

    async def long_press(self, x: int, y: int, *, duration_ms: int = 800) -> None:
        raise NotImplementedError

    async def swipe(self, x1: int, y1: int, x2: int, y2: int, *, duration_ms: int = 300) -> None:
        raise NotImplementedError

    async def input_text(self, text: str) -> None:
        raise NotImplementedError

    async def key(self, keycode: str) -> None:
        raise NotImplementedError

    async def open_app(self, app: str) -> None:
        raise NotImplementedError

    async def list_apps(self) -> list[str]:
        raise NotImplementedError


__all__ = ["DeviceBackend", "DeviceError"]
