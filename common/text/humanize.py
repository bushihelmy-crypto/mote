"""Single authority for compact human-readable number formatting.

Three small pure formatters were each defined once in an unrelated leaf and
render figures the model or the CLI reads:

- ``format_file_size`` (bytes -> ``2KB`` / ``3.4MB``) lived in
  ``executor/tool_result_limit.py`` for the persisted-output notice.
- ``format_elapsed`` (seconds -> ``5.2s`` / ``1m30s``) lived in
  ``executor/tasks/attachment.py`` for the background-task description line.
- ``format_token_count`` (int -> ``840`` / ``3.4k`` / ``12k``) lived in the
  Textual ``status_bar.py`` spinner meta.

They share the "one-decimal, trim the trailing ``.0``" idiom, so they belong
together in the bottom ``common`` layer. Each keeps its own domain wording /
thresholds — this is not a locale-parameterised humanize library, just the three
forms the codebase actually renders.

Zero dependencies beyond the stdlib; no I/O, no provider shapes, no rendering.
"""
from __future__ import annotations


def _one_decimal(value: float) -> str:
    """``value.toFixed(1)`` with a trailing ``.0`` stripped (CC formatting)."""
    return f"{value:.1f}".removesuffix(".0")


def format_file_size(size_in_bytes: int) -> str:
    """Human-readable byte size, matching CC ``formatFileSize``.

    Bytes below 1 KB, else KB / MB / GB with one decimal (trailing ``.0``
    stripped) so the persisted-output message reads like CC's.
    """
    kb = size_in_bytes / 1024
    if kb < 1:
        return f"{size_in_bytes} bytes"
    if kb < 1024:
        return f"{_one_decimal(kb)}KB"
    mb = kb / 1024
    if mb < 1024:
        return f"{_one_decimal(mb)}MB"
    gb = mb / 1024
    return f"{_one_decimal(gb)}GB"


def format_elapsed(seconds: float) -> str:
    """Format elapsed *seconds* as a human-readable string (``5.2s`` / ``1m30s``)."""
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes = int(seconds) // 60
    secs = seconds - minutes * 60
    return f"{minutes}m{secs:.0f}s"


def format_token_count(n: int) -> str:
    """Compact token count for the live spinner meta (``3.4k`` / ``12k`` / ``840``)."""
    if n < 1000:
        return str(n)
    k = n / 1000
    return f"{k:.0f}k" if k >= 10 else f"{k:.1f}k"


__all__ = ["format_file_size", "format_elapsed", "format_token_count"]
