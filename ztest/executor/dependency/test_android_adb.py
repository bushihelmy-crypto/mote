"""Tests for AndroidAdbBackend: asserts the adb argv each op emits (fake subprocess)."""
from __future__ import annotations

import asyncio

import pytest

from mote.runtime.interactive.device import android_adb
from mote.runtime.interactive.device.android_adb import AndroidAdbBackend
from mote.runtime.interactive.device.backend import DeviceError
from mote.ztest.executor.dependency.device_fakes import FAKE_PNG, FAKE_XML


def _run(coro):
    return asyncio.run(coro)


class _RecordingExec:
    """Replacement for ``AndroidAdbBackend._exec`` recording argv + scripting replies."""

    def __init__(self):
        self.calls: list[list[str]] = []
        # Map ``tuple(args-after-adb)`` → (rc, out_bytes, err_bytes).
        self.replies: dict[tuple, tuple[int, bytes, bytes]] = {}
        self.default = (0, b"", b"")

    def reply(self, *args: str, rc: int = 0, out: bytes = b"", err: bytes = b""):
        self.replies[tuple(args)] = (rc, out, err)

    async def __call__(self, argv, *, timeout=android_adb._DEFAULT_TIMEOUT):
        self.calls.append(argv)
        # argv is ``[adb_path, (-s serial)?, *args]`` — strip the adb prefix.
        rest = argv[1:]
        if rest[:1] == ["-s"]:
            rest = rest[2:]
        return self.replies.get(tuple(rest), self.default)


@pytest.fixture
def backend_and_exec(monkeypatch):
    backend = AndroidAdbBackend(adb_path="adb", serial="emulator-5554")
    rec = _RecordingExec()
    monkeypatch.setattr(backend, "_exec", rec)
    return backend, rec


def _last(rec: _RecordingExec) -> list[str]:
    return rec.calls[-1]


def test_available_reflects_which(monkeypatch):
    monkeypatch.setattr(android_adb.shutil, "which", lambda _: "/usr/bin/adb")
    assert AndroidAdbBackend().available is True
    monkeypatch.setattr(android_adb.shutil, "which", lambda _: None)
    assert AndroidAdbBackend().available is False


def test_adb_argv_includes_serial():
    b = AndroidAdbBackend(adb_path="adb", serial="XYZ")
    assert b._adb("shell", "input", "tap") == [
        "adb",
        "-s",
        "XYZ",
        "shell",
        "input",
        "tap",
    ]
    b2 = AndroidAdbBackend(adb_path="adb", serial="")
    assert b2._adb("shell") == ["adb", "shell"]


def test_tap_emits_input_tap(backend_and_exec):
    backend, rec = backend_and_exec
    _run(backend.tap(100, 200))
    assert _last(rec) == [
        "adb",
        "-s",
        "emulator-5554",
        "shell",
        "input",
        "tap",
        "100",
        "200",
    ]


def test_long_press_is_zero_distance_swipe(backend_and_exec):
    backend, rec = backend_and_exec
    _run(backend.long_press(50, 60, duration_ms=900))
    assert _last(rec)[4:] == ["input", "swipe", "50", "60", "50", "60", "900"]


def test_swipe_emits_input_swipe(backend_and_exec):
    backend, rec = backend_and_exec
    _run(backend.swipe(1, 2, 3, 4, duration_ms=250))
    assert _last(rec)[4:] == ["input", "swipe", "1", "2", "3", "4", "250"]


def test_input_text_ascii_uses_input_text_with_space_escape(backend_and_exec):
    backend, rec = backend_and_exec
    _run(backend.input_text("hello world"))
    assert _last(rec)[4:] == ["input", "text", "hello%sworld"]


def test_input_text_non_ascii_uses_ime_broadcast(backend_and_exec):
    backend, rec = backend_and_exec
    _run(backend.input_text("中文测试"))
    assert _last(rec)[4:] == [
        "am",
        "broadcast",
        "-a",
        "ADB_INPUT_TEXT",
        "--es",
        "msg",
        "中文测试",
    ]


def test_input_text_empty_is_noop(backend_and_exec):
    backend, rec = backend_and_exec
    _run(backend.input_text(""))
    assert rec.calls == []


def test_key_maps_alias_to_keycode(backend_and_exec):
    backend, rec = backend_and_exec
    _run(backend.key("BACK"))
    assert _last(rec)[4:] == ["input", "keyevent", "KEYCODE_BACK"]
    _run(backend.key("KEYCODE_ENTER"))
    assert _last(rec)[4:] == ["input", "keyevent", "KEYCODE_ENTER"]


def test_open_app_uses_monkey_launcher(backend_and_exec):
    backend, rec = backend_and_exec
    _run(backend.open_app("com.android.settings"))
    assert _last(rec)[4:] == [
        "monkey",
        "-p",
        "com.android.settings",
        "-c",
        "android.intent.category.LAUNCHER",
        "1",
    ]


def test_screenshot_returns_png_bytes(backend_and_exec):
    backend, rec = backend_and_exec
    rec.reply("exec-out", "screencap", "-p", out=FAKE_PNG)
    data = _run(backend.screenshot())
    assert data == FAKE_PNG


def test_screenshot_empty_raises(backend_and_exec):
    backend, rec = backend_and_exec
    rec.reply("exec-out", "screencap", "-p", out=b"")
    with pytest.raises(DeviceError):
        _run(backend.screenshot())


def test_dump_outline_parses_xml(backend_and_exec):
    backend, rec = backend_and_exec
    rec.reply("shell", "cat", android_adb._DUMP_PATH, out=FAKE_XML.encode())
    outline = _run(backend.dump_outline())
    assert outline.width == 1080
    assert outline.root is not None
    # The dump path was written first, then read.
    dumped = [c for c in rec.calls if c[4:7] == ["uiautomator", "dump", android_adb._DUMP_PATH]]
    assert dumped


def test_dump_outline_degrades_to_empty_on_error(backend_and_exec):
    backend, rec = backend_and_exec
    rec.reply("shell", "uiautomator", "dump", android_adb._DUMP_PATH, rc=1, err=b"no window")
    outline = _run(backend.dump_outline())
    assert outline.root is None  # empty, not an exception


def test_screen_size_parses_wm_size(backend_and_exec):
    backend, rec = backend_and_exec
    rec.reply("shell", "wm", "size", out=b"Physical size: 1080x2340\n")
    assert _run(backend.screen_size()) == (1080, 2340)


def test_list_apps_strips_package_prefix(backend_and_exec):
    backend, rec = backend_and_exec
    rec.reply(
        "shell",
        "pm",
        "list",
        "packages",
        out=b"package:com.android.settings\npackage:com.example.app\n",
    )
    assert _run(backend.list_apps()) == ["com.android.settings", "com.example.app"]


def test_nonzero_exit_raises_device_error(backend_and_exec):
    backend, rec = backend_and_exec
    rec.reply("shell", "input", "tap", "1", "2", rc=1, err=b"error: device offline")
    with pytest.raises(DeviceError) as ei:
        _run(backend.tap(1, 2))
    assert "device offline" in str(ei.value)


def test_start_uses_existing_device(backend_and_exec, monkeypatch):
    backend, rec = backend_and_exec
    monkeypatch.setattr(android_adb.shutil, "which", lambda _: "/usr/bin/adb")
    rec.reply("get-state", out=b"device\n")
    _run(backend.start())  # no raise, no emulator launched
    assert backend._emulator_proc is None


def test_start_strict_mode_raises_without_device(monkeypatch):
    # auto_start_emulator=False keeps the strict "physical device required" path.
    backend = AndroidAdbBackend(serial="X", auto_start_emulator=False)
    rec = _RecordingExec()
    monkeypatch.setattr(backend, "_exec", rec)
    monkeypatch.setattr(android_adb.shutil, "which", lambda _: "/usr/bin/adb")
    rec.reply("get-state", rc=1, err=b"no devices")
    with pytest.raises(DeviceError):
        _run(backend.start())


class _FakeProc:
    """Stand-in for the emulator subprocess (never actually launched)."""

    def __init__(self):
        self.returncode = None
        self.terminated = False
        self.killed = False

    def terminate(self):
        self.terminated = True
        self.returncode = 0

    def kill(self):  # pragma: no cover - only on timeout path
        self.killed = True

    async def wait(self):
        return 0


def _boot_backend(monkeypatch, *, avds, avd_name="", exit_early=False):
    """Build a backend whose emulator launch is faked; return (backend, rec, holder)."""
    backend = AndroidAdbBackend(serial="emulator-5554", avd_name=avd_name, emulator_boot_timeout=5.0)
    rec = _RecordingExec()
    monkeypatch.setattr(backend, "_exec", rec)
    monkeypatch.setattr(android_adb.shutil, "which", lambda _: "/usr/bin/x")
    # emulator -list-avds is dispatched via _exec with argv=[emulator, -list-avds];
    # RecordingExec strips argv[0] then (no -s) → key ("-list-avds",).
    rec.reply("-list-avds", out=("\n".join(avds)).encode())
    launched: dict = {}

    async def fake_spawn(*argv, **kw):
        launched["argv"] = list(argv)
        proc = _FakeProc()
        if exit_early:
            proc.returncode = 1
        launched["proc"] = proc
        return proc

    monkeypatch.setattr(android_adb.asyncio, "create_subprocess_exec", fake_spawn)
    return backend, rec, launched


def test_start_boots_emulator_when_no_device(monkeypatch):
    backend, rec, launched = _boot_backend(monkeypatch, avds=["Pixel_6", "Tablet"])
    # No device at first; after "boot" get-state reports device + boot_completed=1.
    rec.reply("get-state", out=b"device\n")  # RecordingExec returns same reply each time
    rec.reply("shell", "getprop", "sys.boot_completed", out=b"1\n")
    # Force the initial probe to see "no device": start() calls _device_online()
    # which reads get-state; we want first False then True. Simulate by making the
    # first get-state empty via a call counter.
    calls = {"n": 0}
    real = rec.replies[("get-state",)]

    async def gated(argv, *, timeout=android_adb._DEFAULT_TIMEOUT):
        rec.calls.append(argv)
        rest = argv[1:]
        if rest[:1] == ["-s"]:
            rest = rest[2:]
        key = tuple(rest)
        if key == ("get-state",):
            calls["n"] += 1
            return real if calls["n"] > 1 else (0, b"unknown\n", b"")
        return rec.replies.get(key, rec.default)

    monkeypatch.setattr(backend, "_exec", gated)
    _run(backend.start())
    # First AVD chosen, emulator launched with -avd.
    assert launched["argv"][:3] == [backend._emulator_path, "-avd", "Pixel_6"]
    assert backend._emulator_proc is launched["proc"]


def test_start_picks_named_avd(monkeypatch):
    backend, rec, launched = _boot_backend(monkeypatch, avds=["A", "B"], avd_name="B")
    rec.reply("get-state", out=b"device\n")
    rec.reply("shell", "getprop", "sys.boot_completed", out=b"1\n")
    calls = {"n": 0}

    async def gated(argv, *, timeout=android_adb._DEFAULT_TIMEOUT):
        rec.calls.append(argv)
        rest = argv[1:]
        if rest[:1] == ["-s"]:
            rest = rest[2:]
        key = tuple(rest)
        if key == ("get-state",):
            calls["n"] += 1
            return (0, b"device\n", b"") if calls["n"] > 1 else (0, b"unknown\n", b"")
        return rec.replies.get(key, rec.default)

    monkeypatch.setattr(backend, "_exec", gated)
    _run(backend.start())
    assert launched["argv"][2] == "B"


def test_start_no_avds_raises(monkeypatch):
    backend, rec, launched = _boot_backend(monkeypatch, avds=[])
    rec.reply("get-state", out=b"unknown\n")  # never online
    with pytest.raises(DeviceError) as ei:
        _run(backend.start())
    assert "AVD" in str(ei.value)
    assert "argv" not in launched  # never even spawned


def test_start_missing_emulator_binary_raises(monkeypatch):
    backend = AndroidAdbBackend(serial="X")
    rec = _RecordingExec()
    monkeypatch.setattr(backend, "_exec", rec)
    rec.reply("get-state", out=b"unknown\n")
    # adb present, emulator absent.
    monkeypatch.setattr(android_adb.shutil, "which", lambda p: "/usr/bin/adb" if p == "adb" else None)
    with pytest.raises(DeviceError) as ei:
        _run(backend.start())
    assert "emulator" in str(ei.value)


def test_start_emulator_exits_early_raises(monkeypatch):
    backend, rec, launched = _boot_backend(monkeypatch, avds=["A"], exit_early=True)
    rec.reply("get-state", out=b"unknown\n")  # device never comes online
    with pytest.raises(DeviceError) as ei:
        _run(backend.start())
    assert "exited early" in str(ei.value)


def test_shutdown_terminates_launched_emulator(monkeypatch):
    backend = AndroidAdbBackend(serial="X")
    proc = _FakeProc()
    backend._emulator_proc = proc
    _run(backend.shutdown())
    assert proc.terminated is True
    assert backend._emulator_proc is None


def test_shutdown_noop_when_no_emulator():
    backend = AndroidAdbBackend(serial="X")
    _run(backend.shutdown())  # no raise, nothing to reap
    assert backend._emulator_proc is None
