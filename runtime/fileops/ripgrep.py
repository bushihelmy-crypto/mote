"""Pinned ripgrep runtime dependency for candidate discovery only."""

from __future__ import annotations

import os
import platform
import shutil
import sys
from pathlib import Path
from typing import Optional

_ASSET_ROOT = Path(__file__).resolve().parent / "assets" / "ripgrep"
_LINUX_X86_64 = _ASSET_ROOT / "x86_64-linux" / "rg"


def find_ripgrep() -> Optional[str]:
    machine = platform.machine().lower()
    if (
        sys.platform.startswith("linux")
        and machine in {"amd64", "x86_64"}
        and _LINUX_X86_64.is_file()
        and os.access(_LINUX_X86_64, os.X_OK)
    ):
        return str(_LINUX_X86_64)
    return shutil.which("rg")


__all__ = ["find_ripgrep"]
