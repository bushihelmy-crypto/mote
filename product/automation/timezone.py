"""Product-owned selection of the local IANA timezone identity."""

from __future__ import annotations

import os
from pathlib import Path
from zoneinfo import ZoneInfo


def system_timezone_name() -> str:
    candidates = [os.environ.get("TZ", "")]
    try:
        resolved = Path("/etc/localtime").resolve(strict=True)
        marker = "/zoneinfo/"
        if marker in str(resolved):
            candidates.append(str(resolved).split(marker, 1)[1])
    except OSError:
        pass
    try:
        candidates.append(Path("/etc/timezone").read_text(encoding="utf-8").strip())
    except OSError:
        pass
    for candidate in candidates:
        if not candidate:
            continue
        try:
            ZoneInfo(candidate)
        except (KeyError, ValueError):
            continue
        return candidate
    return "UTC"


__all__ = ["system_timezone_name"]
