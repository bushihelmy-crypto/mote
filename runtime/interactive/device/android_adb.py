"""AndroidAdbBackend — the first concrete :class:`DeviceBackend` (adb-reachable Android).

Drives any device the local ``adb`` can reach: a USB phone, an emulator
(``emulator-5554``), or a ``adb connect host:port`` TCP / cloud-phone target
(all selected by ``serial`` via ``adb -s``). The mechanism is deliberately thin —
a handful of ``adb`` invocations — mirroring MobileAgent's ~150-line adb wrapper;
the heavy a11y-outline stabilization lives in the backend-agnostic
:mod:`mote.runtime.interactive.device.outline`.

Every subprocess goes through one binary-safe choke method :meth:`_exec` (argv,
never a shell string — no injection surface, and ``screencap`` PNG bytes survive
untouched). adb is a *trusted external binary* run directly (NOT inside the code
sandbox, which is for untrusted code — adb needs USB / device access); the
executor's PreToolUse permission gate still governs the DeviceUse tool that
drives it.
"""
from __future__ import annotations

import asyncio
import shutil

from loguru import logger

from mote.runtime.interactive.device.base import DeviceBackend, DeviceError
from mote.runtime.interactive.device.outline import RawOutline, parse_uiautomator_xml

# Where uiautomator writes its dump on the device before we read it back.
_DUMP_PATH = "/sdcard/window_dump.xml"
# ADBKeyBoard IME broadcast action — the only reliable way to type non-ASCII
# (plain ``input text`` cannot emit CJK). Requires the ADBKeyBoard APK installed
# and selected as the active IME on the device.
_IME_ACTION = "ADB_INPUT_TEXT"
_DEFAULT_TIMEOUT = 30.0

# A few friendly key aliases → Android keycodes (the model may say "BACK").
_KEY_ALIASES = {
    "BACK": "KEYCODE_BACK",
    "HOME": "KEYCODE_HOME",
    "ENTER": "KEYCODE_ENTER",
    "MENU": "KEYCODE_MENU",
    "POWER": "KEYCODE_POWER",
    "APP_SWITCH": "KEYCODE_APP_SWITCH",
    "RECENTS": "KEYCODE_APP_SWITCH",
    "DELETE": "KEYCODE_DEL",
    "DEL": "KEYCODE_DEL",
    "TAB": "KEYCODE_TAB",
    "VOLUME_UP": "KEYCODE_VOLUME_UP",
    "VOLUME_DOWN": "KEYCODE_VOLUME_DOWN",
}


class AndroidAdbBackend(DeviceBackend):
    """Observe/act on an adb-reachable Android device via the ``adb`` CLI."""

    name = "android"

    def __init__(
        self,
        *,
        adb_path: str = "adb",
        serial: str = "",
        auto_start_emulator: bool = True,
        emulator_path: str = "emulator",
        avd_name: str = "",
        emulator_args: list[str] | None = None,
        emulator_boot_timeout: float = 120.0,
    ) -> None:
        self._adb_path = adb_path
        self._serial = serial
        self._auto_start_emulator = auto_start_emulator
        self._emulator_path = emulator_path
        self._avd_name = avd_name
        self._emulator_args = list(emulator_args or [])
        self._emulator_boot_timeout = emulator_boot_timeout
        # Handle to a subprocess we launched, so shutdown can reap it. Stays None
        # when we attach to a pre-existing (physical/manual-emulator) device.
        self._emulator_proc: asyncio.subprocess.Process | None = None

    @property
    def available(self) -> bool:
        """Whether the ``adb`` binary resolves (on PATH or an absolute path)."""
        return shutil.which(self._adb_path) is not None

    # --- subprocess plumbing ----------------------------------------------

    def _adb(self, *args: str) -> list[str]:
        """Build the ``adb [-s serial] <args...>`` argv."""
        argv = [self._adb_path]
        if self._serial:
            argv += ["-s", self._serial]
        argv += list(args)
        return argv

    async def _exec(self, argv: list[str], *, timeout: float = _DEFAULT_TIMEOUT) -> tuple[int, bytes, bytes]:
        """Run *argv* as a subprocess (binary-safe); return ``(rc, out, err)``.

        The single choke point for every adb call — argv-only (no shell), so
        there is no injection surface and ``screencap`` PNG bytes are preserved.
        """
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as e:
            raise DeviceError(f"adb binary not found: {argv[0]!r}") from e
        except OSError as e:  # pragma: no cover - spawn failure is environment-specific
            raise DeviceError(f"failed to launch adb: {e}") from e
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError as e:
            proc.kill()
            raise DeviceError(f"adb command timed out after {timeout}s: {' '.join(argv[1:])}") from e
        return proc.returncode or 0, out, err

    async def _adb_ok(self, *args: str, timeout: float = _DEFAULT_TIMEOUT) -> bytes:
        """Run ``adb <args>`` and return stdout bytes; raise on non-zero exit."""
        rc, out, err = await self._exec(self._adb(*args), timeout=timeout)
        if rc != 0:
            msg = err.decode("utf-8", errors="replace").strip() or f"exit {rc}"
            raise DeviceError(f"adb {' '.join(args)} failed: {msg}")
        return out

    async def _adb_text(self, *args: str, timeout: float = _DEFAULT_TIMEOUT) -> str:
        return (await self._adb_ok(*args, timeout=timeout)).decode("utf-8", errors="replace")

    # --- lifecycle ---------------------------------------------------------

    async def start(self) -> None:
        """Ensure a device is reachable (idempotent); boot an emulator if none.

        First probes ``adb get-state``. When no device is reachable AND
        ``auto_start_emulator`` is set, it degrades gracefully by booting a local
        Android emulator (AVD) and waiting for it to come online, rather than
        failing outright. A physical device / manually-started emulator that is
        already present is always used as-is (no emulator is launched).
        """
        if not self.available:
            raise DeviceError(f"adb binary not found: {self._adb_path!r}")
        if await self._device_online():
            return
        if not self._auto_start_emulator:
            raise DeviceError("no adb device reachable")
        await self._boot_emulator()

    async def _device_online(self) -> bool:
        """Whether ``adb get-state`` reports a connected/authorized device."""
        rc, out, _ = await self._exec(self._adb("get-state"))
        return rc == 0 and out.decode("utf-8", errors="replace").strip() == "device"

    async def _list_avds(self) -> list[str]:
        """Return the AVD names ``emulator -list-avds`` reports (empty on failure)."""
        try:
            rc, out, _ = await self._exec([self._emulator_path, "-list-avds"])
        except DeviceError:
            return []
        if rc != 0:
            return []
        return [ln.strip() for ln in out.decode("utf-8", errors="replace").splitlines() if ln.strip()]

    async def _boot_emulator(self) -> None:
        """Launch a local AVD as a fallback and wait for it to finish booting.

        Selects ``avd_name`` (or the first ``-list-avds`` entry), spawns the
        ``emulator`` binary detached (its own process, not through a shell), then
        polls ``sys.boot_completed`` until the device is up or the boot timeout
        elapses. Raises :class:`DeviceError` when no emulator/AVD is available or
        the boot does not complete in time.
        """
        if shutil.which(self._emulator_path) is None:
            raise DeviceError(f"no adb device reachable and emulator binary not found: {self._emulator_path!r}")
        avd = self._avd_name
        if not avd:
            avds = await self._list_avds()
            if not avds:
                raise DeviceError(
                    "no adb device reachable and no Android AVD found to auto-start "
                    "(create one with `avdmanager`, or attach a physical device)."
                )
            avd = avds[0]
        argv = [self._emulator_path, "-avd", avd, *self._emulator_args]
        logger.info("DeviceUse: no device reachable — booting emulator AVD {!r}", avd)
        try:
            self._emulator_proc = await asyncio.create_subprocess_exec(
                *argv,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
        except (FileNotFoundError, OSError) as e:
            raise DeviceError(f"failed to launch emulator: {e}") from e
        await self._wait_for_boot(avd)

    async def _wait_for_boot(self, avd: str) -> None:
        """Poll until the emulator reports ``sys.boot_completed`` or timeout."""
        deadline = asyncio.get_event_loop().time() + self._emulator_boot_timeout
        # First wait for the device slot to appear (adb sees it), then for the
        # OS boot to complete (getprop sys.boot_completed == 1).
        while asyncio.get_event_loop().time() < deadline:
            if self._emulator_proc is not None and self._emulator_proc.returncode is not None:
                raise DeviceError(f"emulator {avd!r} exited early (code {self._emulator_proc.returncode})")
            if await self._device_online():
                rc, out, _ = await self._exec(self._adb("shell", "getprop", "sys.boot_completed"), timeout=10.0)
                if rc == 0 and out.decode("utf-8", errors="replace").strip() == "1":
                    logger.info("DeviceUse: emulator AVD {!r} booted", avd)
                    return
            await asyncio.sleep(2.0)
        raise DeviceError(f"emulator {avd!r} did not finish booting within {self._emulator_boot_timeout}s")

    async def shutdown(self) -> None:
        """Release the device; terminate an emulator we launched (best-effort)."""
        proc = self._emulator_proc
        if proc is None:
            return
        self._emulator_proc = None
        if proc.returncode is not None:
            return
        try:
            proc.terminate()
            await asyncio.wait_for(proc.wait(), timeout=10.0)
        except (asyncio.TimeoutError, ProcessLookupError, OSError):
            try:
                proc.kill()
            except (ProcessLookupError, OSError):  # pragma: no cover - race on reap
                pass

    # --- observation -------------------------------------------------------

    async def screenshot(self) -> bytes:
        """Capture the screen as PNG bytes via ``adb exec-out screencap -p``."""
        out = await self._adb_ok("exec-out", "screencap", "-p")
        if not out:
            raise DeviceError("screencap returned no data")
        return out

    async def dump_outline(self) -> RawOutline:
        """Dump the a11y tree via ``uiautomator dump`` and normalize it.

        A secured / game / custom-drawn surface may make ``uiautomator dump``
        fail or yield nothing usable; that degrades to an empty
        :class:`RawOutline` (never raises) so the session falls back to the
        screenshot for pure-visual grounding.
        """
        try:
            await self._adb_ok("shell", "uiautomator", "dump", _DUMP_PATH)
            xml = await self._adb_text("shell", "cat", _DUMP_PATH)
        except DeviceError:
            return RawOutline()
        return parse_uiautomator_xml(xml)

    async def screen_size(self) -> tuple[int, int]:
        """Return ``(width, height)`` in pixels via ``adb shell wm size``."""
        text = await self._adb_text("shell", "wm", "size")
        # Output like: "Physical size: 1080x2340" (may also list "Override size").
        for line in text.splitlines():
            _, _, dims = line.partition(":")
            dims = dims.strip()
            if "x" in dims:
                w, _, h = dims.partition("x")
                try:
                    return int(w.strip()), int(h.strip())
                except ValueError:
                    continue
        raise DeviceError(f"could not parse screen size from: {text!r}")

    # --- actions -----------------------------------------------------------

    async def tap(self, x: int, y: int) -> None:
        await self._adb_ok("shell", "input", "tap", str(x), str(y))

    async def long_press(self, x: int, y: int, *, duration_ms: int = 800) -> None:
        # A long-press is a zero-distance swipe held for ``duration_ms``.
        await self._adb_ok("shell", "input", "swipe", str(x), str(y), str(x), str(y), str(duration_ms))

    async def swipe(self, x1: int, y1: int, x2: int, y2: int, *, duration_ms: int = 300) -> None:
        await self._adb_ok(
            "shell",
            "input",
            "swipe",
            str(x1),
            str(y1),
            str(x2),
            str(y2),
            str(duration_ms),
        )

    async def input_text(self, text: str) -> None:
        """Type *text* — ASCII via ``input text``, non-ASCII via ADBKeyBoard IME."""
        if not text:
            return
        if text.isascii():
            # ``input text`` treats spaces specially — encode them as %s.
            await self._adb_ok("shell", "input", "text", text.replace(" ", "%s"))
        else:
            # CJK / emoji: broadcast to the ADBKeyBoard IME (must be installed +
            # active). ``--es msg <text>`` carries the string.
            await self._adb_ok("shell", "am", "broadcast", "-a", _IME_ACTION, "--es", "msg", text)

    async def key(self, keycode: str) -> None:
        code = _KEY_ALIASES.get(keycode.strip().upper(), keycode.strip())
        await self._adb_ok("shell", "input", "keyevent", code)

    async def open_app(self, app: str) -> None:
        """Launch *app* by package name via ``monkey`` LAUNCHER intent."""
        await self._adb_ok(
            "shell",
            "monkey",
            "-p",
            app,
            "-c",
            "android.intent.category.LAUNCHER",
            "1",
        )

    async def list_apps(self) -> list[str]:
        """Return installed package names via ``pm list packages``."""
        text = await self._adb_text("shell", "pm", "list", "packages")
        pkgs: list[str] = []
        for line in text.splitlines():
            line = line.strip()
            if line.startswith("package:"):
                pkgs.append(line[len("package:") :])
            elif line:
                pkgs.append(line)
        return pkgs


__all__ = ["AndroidAdbBackend"]
