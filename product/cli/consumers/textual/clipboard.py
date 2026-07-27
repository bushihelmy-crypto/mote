#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Pure OS clipboard helpers for the Textual host.

:class:`~mote.product.cli.consumers.textual.app.MoteApp` copies the transcript
selection through Textual's default OSC 52 path *except* on a local WSL host,
where it writes the Windows clipboard natively (VS Code's integrated terminal
*appends* OSC 52 payloads instead of replacing, so repeated copies doubled). The
host detection and the native write are pure functions of the environment / the
text — no ``App`` state — so they live here, leaving the app with only the
event-handler + widget-mutation concerns.
"""

from __future__ import annotations

import os
import subprocess


def detect_wsl_clipboard() -> bool:
    """Whether to route clipboard writes through the Windows clipboard natively.

    True on a *local* WSL host (there is a Windows clipboard reachable via
    ``powershell.exe``). False under SSH — there OSC 52 forwards the copy to
    the operator's own terminal and ``powershell.exe`` would target the wrong
    (or no) clipboard.
    """
    if os.environ.get("SSH_CONNECTION") or os.environ.get("SSH_TTY"):
        return False
    if os.environ.get("WSL_DISTRO_NAME") or os.environ.get("WSL_INTEROP"):
        return True
    try:
        with open("/proc/version", "r", encoding="utf-8", errors="ignore") as fh:
            return "microsoft" in fh.read().lower()
    except OSError:
        return False


def native_copy(text: str) -> bool:
    """Set the Windows clipboard via ``powershell.exe``; return whether it ran.

    Fire-and-forget (the launch returns immediately, so the UI thread never
    blocks on the ~sub-second PowerShell startup). ``InputEncoding`` is forced
    to UTF-8 so multi-byte glyphs (bullets, CJK) survive the pipe intact.
    """
    try:
        proc = subprocess.Popen(
            [
                "powershell.exe",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                "[Console]::InputEncoding=[System.Text.Encoding]::UTF8;"
                "Set-Clipboard -Value ([Console]::In.ReadToEnd())",
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError:
        return False  # powershell.exe unavailable → fall back to OSC 52
    try:
        assert proc.stdin is not None
        proc.stdin.write(text.encode("utf-8"))
        proc.stdin.close()
    except OSError:
        return False
    return True


__all__ = ["detect_wsl_clipboard", "native_copy"]
