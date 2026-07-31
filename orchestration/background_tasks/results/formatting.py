"""Formatting for background-task results."""

from __future__ import annotations


def format_elapsed(seconds: float) -> str:
    """Format elapsed seconds for a task status description."""
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes = int(seconds) // 60
    secs = seconds - minutes * 60
    return f"{minutes}m{secs:.0f}s"


__all__ = ["format_elapsed"]
