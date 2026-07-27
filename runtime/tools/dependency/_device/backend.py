"""DeviceBackend — the pluggable device-driver strategy (mirrors SandboxBackend).

A :class:`DeviceBackend` knows *how* to observe and act on one "device" (an
Android phone over adb today; a desktop / iOS / cloud phone tomorrow). The
device-independent :class:`~mote.runtime.tools.dependency._device.session.DeviceSession`
owns *what* to do (snapshot bookkeeping, ref resolution, serialization) and
delegates the mechanism here.

The contract is deliberately narrow and screenshot-first: a backend can always
return a screenshot (the robust floor for pure-visual grounding) and a
normalized a11y outline (:class:`RawOutline`, the accelerator). Actions take
device pixels (the session resolves a ``@e{N}`` ref to a pixel tap point before
calling here) or a semantic target (open_app / key).

Mirrors :class:`mote.runtime.sandbox.backend.SandboxBackend`: a ``name`` + an
``available`` probe + the strategy methods; a :class:`NullDeviceBackend`
graceful-degrade default; and :func:`select_device_backend` choosing the
concrete backend from a :class:`DeviceConfig`.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from mote.runtime.tools.dependency._device.android_adb import AndroidAdbBackend
from mote.runtime.tools.dependency._device.base import DeviceBackend, DeviceError
from mote.runtime.tools.dependency._device.outline import RawOutline

if TYPE_CHECKING:
    from mote.contracts.settings.device import DeviceConfig


class NullDeviceBackend(DeviceBackend):
    """No-device backend: every operation raises :class:`DeviceError`.

    Selected when no real backend is available and the config did not force one
    (graceful degrade), or explicitly via ``backend="none"``. Construction never
    fails, so the DeviceUse tool can be bound; the first action reports cleanly
    that no device is reachable.
    """

    name = "none"

    _MSG = (
        "No device backend is available. Attach an adb-reachable Android device "
        "(USB / emulator / `adb connect host:port` / cloud phone) and ensure the "
        "`adb` binary is installed, or set tools.device.backend explicitly."
    )

    @property
    def available(self) -> bool:
        return True  # the null backend is always "available" as a fallback

    async def start(self) -> None:
        raise DeviceError(self._MSG)

    async def screenshot(self) -> bytes:
        raise DeviceError(self._MSG)

    async def dump_outline(self) -> RawOutline:
        raise DeviceError(self._MSG)

    async def screen_size(self) -> tuple[int, int]:
        raise DeviceError(self._MSG)

    async def tap(self, x: int, y: int) -> None:
        raise DeviceError(self._MSG)

    async def long_press(self, x: int, y: int, *, duration_ms: int = 800) -> None:
        raise DeviceError(self._MSG)

    async def swipe(self, x1: int, y1: int, x2: int, y2: int, *, duration_ms: int = 300) -> None:
        raise DeviceError(self._MSG)

    async def input_text(self, text: str) -> None:
        raise DeviceError(self._MSG)

    async def key(self, keycode: str) -> None:
        raise DeviceError(self._MSG)

    async def open_app(self, app: str) -> None:
        raise DeviceError(self._MSG)

    async def list_apps(self) -> list[str]:
        raise DeviceError(self._MSG)


def select_device_backend(config: "DeviceConfig") -> DeviceBackend:
    """Choose the concrete backend for *config* (graceful-degrade to null).

    * ``none`` → :class:`NullDeviceBackend` (explicitly disabled).
    * ``android`` → the Android adb backend, forced (even if adb is absent, so
      the failure is a clear ``DeviceError`` at first use rather than silent).
    * ``auto`` (default) → the Android adb backend when it is available (adb on
      PATH), else :class:`NullDeviceBackend`.

    The driver contract lives in ``base`` so selection and implementation have
    no import cycle.
    """
    mode = getattr(config, "backend", "auto")
    if mode == "none":
        return NullDeviceBackend()
    android = AndroidAdbBackend(
        adb_path=config.adb_path,
        serial=config.serial,
        auto_start_emulator=config.auto_start_emulator,
        emulator_path=config.emulator_path,
        avd_name=config.avd_name,
        emulator_args=config.emulator_args,
        emulator_boot_timeout=config.emulator_boot_timeout,
    )
    if mode == "android":
        return android
    # auto
    return android if android.available else NullDeviceBackend()


__all__ = [
    "DeviceBackend",
    "NullDeviceBackend",
    "DeviceError",
    "select_device_backend",
]
