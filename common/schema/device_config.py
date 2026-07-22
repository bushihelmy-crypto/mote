"""DeviceConfig — knobs for the DeviceUse tool's device backend.

Pure-data config model (no executor import), lazy-exported from
``mote.common.schema``. Mirrors the browser fingerprint knobs' home in
``config.tools``: the DeviceUse tool reads it via the ``get_device_config``
capability. The first backend is Android over adb, so the fields describe how to
reach an adb device; ``backend`` selects the concrete backend (``auto`` picks
the only real one — Android — when adb is reachable, else a null backend).
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class DeviceConfig(BaseModel):
    """Settings for the DeviceUse tool's pluggable device backend."""

    model_config = ConfigDict(extra="forbid")

    # Which backend drives the "device". ``auto`` (default) selects the Android
    # adb backend when adb is available, else a null (no-device) backend. Set
    # ``android`` to force it, or ``none`` to disable the tool's device access.
    backend: Literal["auto", "android", "none"] = "auto"

    # Path to the ``adb`` binary (Android backend). A bare ``adb`` resolves on
    # PATH; set an absolute path when adb is not on PATH.
    adb_path: str = "adb"

    # Target device serial passed to ``adb -s <serial>`` — selects one device
    # when several are attached. Empty (default) lets adb pick the sole device.
    # A serial naturally covers USB, an emulator (``emulator-5554``), and a
    # ``adb connect host:port`` TCP / cloud-phone target alike.
    serial: str = ""

    # Emulator auto-start fallback: when ``start`` finds NO adb-reachable device,
    # boot a local Android emulator (AVD) rather than failing. ``True`` (default)
    # degrades gracefully to a virtual device on a host with the emulator + AVDs
    # installed; ``False`` keeps the strict "physical device required" behavior.
    auto_start_emulator: bool = True

    # Path to the ``emulator`` binary used to boot an AVD (fallback path). A bare
    # ``emulator`` resolves on PATH (Android SDK ``emulator/`` dir); set an
    # absolute path when it is not on PATH.
    emulator_path: str = "emulator"

    # Which AVD to boot when auto-starting. Empty (default) picks the first AVD
    # ``emulator -list-avds`` reports; set a name to force a specific one.
    avd_name: str = ""

    # Extra flags passed to the ``emulator`` launch (e.g. ``-no-window`` for
    # headless CI, ``-no-snapshot``). Empty by default.
    emulator_args: list[str] = Field(default_factory=list)

    # How long (seconds) to wait for a freshly-booted emulator to come online
    # (``sys.boot_completed``) before giving up.
    emulator_boot_timeout: float = 120.0


__all__ = ["DeviceConfig"]
