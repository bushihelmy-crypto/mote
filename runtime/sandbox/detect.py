"""Backend detection — best-effort host probe for the isolation backend.

Filesystem-first / no-spawn where possible, mirroring ``common.git_state``'s
``find_git_root`` philosophy: we use ``shutil.which`` (a PATH scan, no
subprocess) to decide whether ``bwrap`` is available, plus a platform check.

Never raises: an unprobeable host degrades to ``"none"`` so the runtime can
decide (per ``fail_if_unavailable``) whether that is fatal or a soft passthrough.
"""
from __future__ import annotations

import shutil
import sys

# The literal backend kinds the runtime understands. Kept in sync with
# ``SandboxBackendKind`` in the schema, but defined here to avoid importing the
# schema from a leaf detection module.
_BWRAP = "bwrap"
_NONE = "none"


def bwrap_available() -> bool:
    """True when the ``bwrap`` binary is on PATH and we're on Linux.

    bubblewrap is Linux-only (it relies on user namespaces); on any other
    platform there is no point probing PATH.
    """
    if not sys.platform.startswith("linux"):
        return False
    return shutil.which("bwrap") is not None


def detect_backend(requested: str = "auto") -> str:
    """Resolve the effective backend for *requested* on this host.

    Args:
        requested: ``"auto"`` (probe), ``"bwrap"`` (force — still validated
            against availability), or ``"none"`` (explicit passthrough).

    Returns:
        ``"bwrap"`` when bubblewrap is usable, else ``"none"``. A forced
        ``"bwrap"`` that is not actually available resolves to ``"none"`` here;
        the runtime turns that into a hard error or a warning per
        ``fail_if_unavailable`` (detection never raises).
    """
    if requested == _NONE:
        return _NONE
    if requested in ("auto", _BWRAP):
        return _BWRAP if bwrap_available() else _NONE
    # Unknown value — treat conservatively as no isolation.
    return _NONE


def bwrap_path() -> str | None:
    """Absolute path to the ``bwrap`` binary, or ``None`` if not found."""
    return shutil.which("bwrap")


__all__ = ["detect_backend", "bwrap_available", "bwrap_path"]
