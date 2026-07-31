"""Tests for the DeviceBackend contract: null backend + selection / graceful-degrade."""
from __future__ import annotations

import pytest

from mote.runtime.config.device import DeviceConfig
from mote.runtime.interactive.device import android_adb
from mote.runtime.interactive.device.backend import DeviceError, NullDeviceBackend, select_device_backend


def _run(coro):
    import asyncio

    return asyncio.run(coro)


def test_null_backend_is_available_but_every_op_raises():
    null = NullDeviceBackend()
    assert null.name == "none"
    assert null.available is True  # always a valid fallback
    with pytest.raises(DeviceError):
        _run(null.start())
    with pytest.raises(DeviceError):
        _run(null.screenshot())
    with pytest.raises(DeviceError):
        _run(null.dump_outline())
    with pytest.raises(DeviceError):
        _run(null.tap(1, 2))
    with pytest.raises(DeviceError):
        _run(null.input_text("x"))
    with pytest.raises(DeviceError):
        _run(null.list_apps())


def test_select_none_returns_null():
    backend = select_device_backend(DeviceConfig(backend="none"))
    assert isinstance(backend, NullDeviceBackend)


def test_select_android_forces_android_even_when_adb_absent(monkeypatch):
    # Force adb "unavailable" — ``android`` must still return the real backend so
    # the failure is a clear DeviceError at first use, not a silent null.
    monkeypatch.setattr(android_adb.shutil, "which", lambda _: None)
    backend = select_device_backend(DeviceConfig(backend="android"))
    assert isinstance(backend, android_adb.AndroidAdbBackend)
    assert backend.available is False


def test_select_auto_picks_android_when_adb_available(monkeypatch):
    monkeypatch.setattr(android_adb.shutil, "which", lambda _: "/usr/bin/adb")
    backend = select_device_backend(DeviceConfig(backend="auto"))
    assert isinstance(backend, android_adb.AndroidAdbBackend)


def test_select_auto_degrades_to_null_when_adb_absent(monkeypatch):
    monkeypatch.setattr(android_adb.shutil, "which", lambda _: None)
    backend = select_device_backend(DeviceConfig(backend="auto"))
    assert isinstance(backend, NullDeviceBackend)


def test_select_threads_adb_path_and_serial(monkeypatch):
    monkeypatch.setattr(android_adb.shutil, "which", lambda _: "/opt/adb")
    backend = select_device_backend(DeviceConfig(backend="android", adb_path="/opt/adb", serial="emulator-5554"))
    assert isinstance(backend, android_adb.AndroidAdbBackend)
    assert backend._adb("shell", "x") == [
        "/opt/adb",
        "-s",
        "emulator-5554",
        "shell",
        "x",
    ]
