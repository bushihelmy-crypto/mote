"""Deterministic ANSI escape stripping for captured output.

Terminal-emitting programs (pytest / ruff / git on a TTY, ipykernel tracebacks)
sprinkle CSI escape sequences — colour codes, cursor moves — into their output.
Structural parsers and the model both want the clean text. This regex + strip was
copy-pasted verbatim in two executor leaves (``compress/base.py`` and
``dependency/_kernel.py``) because neither could import the other's internal; it
now lives once in the bottom ``common`` layer.

Zero dependencies beyond the stdlib.
"""
from __future__ import annotations

import re

# CSI escape sequences: ESC ``[`` , optional parameter bytes ``0-9 ; ? ``,
# optional intermediate bytes `` -/`` , a final byte ``@-~``. Covers colour codes
# and cursor moves emitted by pytest/ruff/git on a TTY and ipykernel tracebacks.
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")


def strip_ansi(text: str) -> str:
    """Remove ANSI escape sequences so structural parsing sees clean text."""
    return _ANSI_RE.sub("", text)


__all__ = ["strip_ansi"]
