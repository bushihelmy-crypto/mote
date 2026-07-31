"""Compact formatting for product presentation."""

from __future__ import annotations


def format_token_count(count: int) -> str:
    """Format a token count for compact status displays."""
    if count < 1000:
        return str(count)
    thousands = count / 1000
    return f"{thousands:.0f}k" if thousands >= 10 else f"{thousands:.1f}k"


__all__ = ["format_token_count"]
